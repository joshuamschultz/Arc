"""CallbackDispatcher: signed, pinned, pointer-only work-event callbacks (SPEC-088 COMP-013).

Modeled on :class:`arcstore.approval_dispatcher.ApprovalNotificationDispatcher`:
it leases due deliveries from the work-event log, attempts each once and
settles it. One attempt is:

1. load the event the delivery points at;
2. re-check the callback URL against the allowlist (a removed origin is refused);
3. withhold the event when its classification is above the entry clearance;
4. resolve the host ONCE and refuse unless every address is allowed, then
   connect to that checked address with the original ``Host`` header and TLS
   SNI name, so a DNS answer that changes after the check is never used;
5. POST the pointer envelope, signed with the dedicated work-events key, with
   no redirects and a 10 s timeout.

A non-2xx answer, a refusal at steps 2 or 4, a timeout or a transport error is a
failed attempt: retried after ``min(30 * 2**(attempts - 1), 3600)`` seconds and
dead at the 10th, which is audited and notified to the operator. Every attempt
writes one ``work_event.callback`` audit event.

Signed bytes: ``b"arc.work-event.v1\\n" + timestamp + b"\\n" + event_id + b"\\n" +
sha256(body).hexdigest()``, where ``timestamp`` is the ``Arc-Timestamp`` header
value verbatim (Unix seconds). With no work-events key there is no dispatcher:
:func:`build_callback_dispatcher` answers ``None`` and nothing is ever sent unsigned.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Protocol

import httpx
from arcstore.work_events import DeliveryOutcome, WorkEvent, WorkEventDelivery, WorkEventLog
from arctrust.audit import AuditEvent, AuditSink
from arctrust.classification import Classification, dominates, parse_classification

from arcui.work_event_envelope import pointer_envelope
from arcui.work_events.policy import CallbackRefusedError, Resolver, checked_addresses

__all__ = [
    "MAX_ATTEMPTS",
    "CallbackDispatcher",
    "CallbackEntry",
    "CallbackPolicyCheck",
    "OperatorNotifier",
    "WorkEventsSigner",
    "backoff_seconds",
    "build_callback_dispatcher",
]

logger = logging.getLogger(__name__)

#: The attempt that fails for the 10th time settles the delivery ``dead``.
MAX_ATTEMPTS = 10
SIGNING_DOMAIN = b"arc.work-event.v1"
REQUEST_TIMEOUT_SECONDS = 10.0
#: Whole-attempt bound. httpx timeouts are per phase, so a receiver that drips
#: bytes is bounded only here; kept under the 60 s delivery lease so a late
#: settle never finds its lease gone and the event sent twice.
ATTEMPT_DEADLINE_SECONDS = 30.0
_ACTOR_DID = "did:arc:system:work-events"
_AUDIT_ACTION = "work_event.callback"
_BATCH = 20


class WorkEventsSigner(Protocol):
    """The COMP-019 capability: signs by reference, never the operator key."""

    @property
    def public_key(self) -> bytes: ...

    @property
    def algorithm(self) -> str: ...

    @property
    def key_id(self) -> str: ...

    def sign(self, message: bytes) -> bytes: ...


class CallbackEntry(Protocol):
    """The allowlist-entry fields an attempt reads."""

    @property
    def allow_private(self) -> bool: ...

    @property
    def clearance(self) -> Classification: ...


class CallbackPolicyCheck(Protocol):
    """``check(url)`` returns the allowing entry or raises ``ValueError``."""

    def check(self, url: str) -> CallbackEntry: ...


#: Awaited once when a delivery goes dead; receives the event id.
OperatorNotifier = Callable[[str], Awaitable[None]]


class _FailedAttemptError(Exception):
    """One failed delivery attempt; ``reason`` is a stable code, never the URL."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def backoff_seconds(attempts: int) -> int:
    """Delay before the next try after ``attempts`` failed attempts: 30, 60, 120, ... 3600."""
    return int(min(30 * 2 ** max(attempts - 1, 0), 3600))


def _signed_message(timestamp: str, event_id: str, body: bytes) -> bytes:
    digest = hashlib.sha256(body).hexdigest()
    return b"\n".join(
        (SIGNING_DOMAIN, timestamp.encode("ascii"), event_id.encode("ascii"), digest.encode())
    )


class CallbackDispatcher:
    """Lease, sign, deliver and settle work-event callbacks on a background poll loop."""

    def __init__(
        self,
        log: WorkEventLog,
        *,
        signer: WorkEventsSigner,
        policy: CallbackPolicyCheck,
        transport: httpx.AsyncBaseTransport,
        resolver: Resolver,
        notify_operator: OperatorNotifier,
        audit_sink: AuditSink,
        clock: Callable[[], datetime],
        worker_id: str = "arcui-work-event-callbacks",
        poll_interval_seconds: float = 1.0,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._log = log
        self._signer = signer
        self._policy = policy
        self._resolver = resolver
        self._notify_operator = notify_operator
        self._audit_sink = audit_sink
        self._clock = clock
        self._worker_id = worker_id
        self._poll_interval = poll_interval_seconds
        # trust_env=False: an environment proxy would resolve the host again.
        self._client = httpx.AsyncClient(
            transport=transport, follow_redirects=False, trust_env=False
        )
        self._task: asyncio.Task[None] | None = None

    async def dispatch_once(self) -> int:
        """Attempt one claimed batch concurrently; return how many were claimed."""
        deliveries = await self._log.claim_deliveries(self._worker_id, _BATCH)
        await asyncio.gather(*(self._attempt(delivery) for delivery in deliveries))
        return len(deliveries)

    async def start(self) -> None:
        """Run the poll loop as one background task; repeated starts are idempotent."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="arcui-work-event-callbacks")

    async def stop(self) -> None:
        """Stop the loop and close the connection pool; a cut lease expires and retries."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise
        await self._client.aclose()

    async def _run(self) -> None:
        while True:
            try:
                claimed = await self.dispatch_once()
            except Exception:  # reason: a store outage must not end the loop
                logger.exception("work-event callbacks: claim failed")
                claimed = 0
            if not claimed:
                await asyncio.sleep(self._poll_interval)

    async def _attempt(self, delivery: WorkEventDelivery) -> None:
        """One attempt, always settled; never raises past the batch."""
        try:
            async with asyncio.timeout(ATTEMPT_DEADLINE_SECONDS):
                event = await self._log.get(delivery.event_id)
                if event is None:
                    await self._settle(delivery, "dead", error="event_missing")
                    return
                await self._deliver(delivery, event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # reason: one delivery's fault must not stop the batch
            logger.exception("work-event callbacks: attempt for %s failed", delivery.event_id)
            await self._failed_unexpectedly(delivery, type(exc).__name__)

    async def _failed_unexpectedly(self, delivery: WorkEventDelivery, reason: str) -> None:
        """Count an unforeseen fault as a failed attempt, so the attempt cap, the
        ``dead`` audit and the operator notice apply to it too. Best effort: a
        store still down leaves the lease to expire and the delivery re-claimed."""
        try:
            await self._failed(delivery, reason)
        except Exception:  # reason: the store is the fault; the lease expiry recovers
            logger.warning("work-event callbacks: could not settle %s", delivery.event_id)

    async def _deliver(self, delivery: WorkEventDelivery, event: WorkEvent) -> None:
        try:
            entry = await asyncio.to_thread(self._policy.check, delivery.callback_url)
        except ValueError as exc:
            await self._failed(delivery, getattr(exc, "reason", "callback_not_allowed"))
            return
        if not _cleared(entry.clearance, event.classification):
            await self._settle(delivery, "withheld", error="above_clearance")
            return
        try:
            request = await self._pinned_request(delivery, event, entry.allow_private)
            # Streamed and closed unread: only the status matters, and a
            # receiver-controlled body is never buffered.
            response = await self._client.send(request, stream=True)
            await response.aclose()
        except _FailedAttemptError as exc:
            await self._failed(delivery, exc.reason)
            return
        except httpx.HTTPError as exc:
            await self._failed(delivery, type(exc).__name__)
            return
        if response.is_success:
            await self._settle(delivery, "delivered", error=None)
        else:
            await self._failed(delivery, f"http_{response.status_code}")

    async def _pinned_request(
        self, delivery: WorkEventDelivery, event: WorkEvent, allow_private: bool
    ) -> httpx.Request:
        """The signed POST addressed to the checked IP, carrying the original name."""
        url = httpx.URL(delivery.callback_url)
        try:
            addresses = await asyncio.to_thread(
                checked_addresses, url.host, self._resolver, allow_private=allow_private
            )
        except CallbackRefusedError as exc:
            raise _FailedAttemptError(exc.reason) from None
        body = json.dumps(pointer_envelope(event), separators=(",", ":")).encode()
        timestamp = str(int(self._clock().timestamp()))
        signature = self._signer.sign(_signed_message(timestamp, event.event_id, body))
        headers = {
            "Host": url.netloc.decode("ascii"),
            "Content-Type": "application/json",
            "Arc-Event-Id": event.event_id,
            "Arc-Timestamp": timestamp,
            "Arc-Key-Id": self._signer.key_id,
            "Arc-Signature": f"{self._signer.algorithm}={base64.b64encode(signature).decode()}",
        }
        return self._client.build_request(
            "POST",
            url.copy_with(host=str(addresses[0])),
            content=body,
            headers=headers,
            extensions={"sni_hostname": url.host},
            timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
        )

    async def _failed(self, delivery: WorkEventDelivery, reason: str) -> None:
        if delivery.attempts >= MAX_ATTEMPTS:
            await self._settle(delivery, "dead", error=reason)
            return
        await self._settle(
            delivery, "retry", error=reason, retry_after_s=backoff_seconds(delivery.attempts)
        )

    async def _settle(
        self,
        delivery: WorkEventDelivery,
        outcome: DeliveryOutcome,
        *,
        error: str | None,
        retry_after_s: float | None = None,
    ) -> None:
        settled = await self._log.settle(
            delivery.event_id,
            outcome=outcome,
            retry_after_s=retry_after_s,
            error=error,
            consumer=self._worker_id,
        )
        recorded = outcome if settled else "lease_lost"
        self._audit(delivery, recorded, error)
        logger.info(
            "work-event callback event=%s attempt=%d outcome=%s reason=%s",
            delivery.event_id,
            delivery.attempts,
            recorded,
            error,
        )
        if settled and outcome == "dead":
            await self._notify(delivery.event_id)

    def _audit(self, delivery: WorkEventDelivery, outcome: str, reason: str | None) -> None:
        event = AuditEvent(
            actor_did=_ACTOR_DID,
            action=_AUDIT_ACTION,
            target=f"work_event:{delivery.event_id}"[:512],
            outcome=outcome,
            extra={"attempt": delivery.attempts, "reason": reason},
        )
        try:
            self._audit_sink.write(event)
        except Exception:  # reason: AU-5, an audit fault never changes delivery state
            logger.warning("work-event callbacks: audit write failed", exc_info=True)

    async def _notify(self, event_id: str) -> None:
        try:
            await self._notify_operator(event_id)
        except Exception:  # reason: the dead record is already settled and audited
            logger.warning("work-event callbacks: operator notice failed", exc_info=True)


def _cleared(clearance: Classification, label: str) -> bool:
    """Fail closed: an unknown event label is never sent."""
    try:
        return dominates(clearance, parse_classification(label, strict=True))
    except ValueError:
        return False


def build_callback_dispatcher(
    log: WorkEventLog,
    *,
    signer: WorkEventsSigner | None,
    policy: CallbackPolicyCheck,
    transport: httpx.AsyncBaseTransport,
    resolver: Resolver,
    notify_operator: OperatorNotifier,
    audit_sink: AuditSink,
    clock: Callable[[], datetime],
) -> CallbackDispatcher | None:
    """The dispatcher, or ``None`` with no work-events key (nothing is sent unsigned)."""
    if signer is None:
        return None
    return CallbackDispatcher(
        log,
        signer=signer,
        policy=policy,
        transport=transport,
        resolver=resolver,
        notify_operator=notify_operator,
        audit_sink=audit_sink,
        clock=clock,
    )
