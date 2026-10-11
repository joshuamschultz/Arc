"""SPEC-088 T-1369 (RED): CallbackDispatcher signs and delivers work-event callbacks.

Covers COMP-013 (dispatcher half) and COMP-019 (signing seam) against REQ-626, REQ-627,
REQ-629, REQ-631 and REQ-632 (DNS rebinding, redirect, operator-key abuse cases), D-848,
D-849.

Contract this file pins for the builder (T-1370), new module
``arcui.work_events.dispatcher``:

* ``CallbackDispatcher(log, *, signer, policy, transport, resolver, notify_operator,
  audit_sink, clock, worker_id="arcui-work-event-callbacks", poll_interval_seconds=1.0)``

  - ``log``: the arcstore ``WorkEventLog`` (claims with ``claim_deliveries``, settles with
    ``settle(event_id, outcome=, retry_after_s=, error=, consumer=)``).
  - ``signer``: the COMP-019 capability (``sign``, ``public_key``, ``algorithm``,
    ``key_id``). Never the operator signer.
  - ``policy``: COMP-013 ``CallbackPolicy``-shaped; ``check(url)`` returns the allowlist
    entry (``origin``, ``allow_private``, ``clearance: Classification``) or raises
    ``ValueError``. Re-checked before every delivery.
  - ``transport``: an ``httpx.AsyncBaseTransport`` (production: the default pool).
  - ``resolver``: ``arcagent.utils.url_security.Resolver`` (``Callable[[str],
    Iterable[str]]``). The dispatcher resolves the callback host ONCE per attempt,
    refuses unless every address is public (or private with ``allow_private``; link-local
    and metadata addresses never), then CONNECTS TO THAT CHECKED ADDRESS: the request the
    transport receives has the checked IP literal as its URL host, the original host in
    the ``Host`` header and in the ``sni_hostname`` request extension. It never hands the
    transport a hostname to resolve again (DNS rebinding).
  - ``notify_operator``: async callable, awaited once when a delivery goes dead; its
    arguments name the event id.
  - ``audit_sink``: ``arctrust.audit.AuditSink``; every attempt writes one event with
    ``action == "work_event.callback"``, ``target`` containing the event id and
    ``outcome`` in ``delivered | retry | withheld | dead``.
  - ``clock``: ``Callable[[], datetime]`` (timezone-aware); the source of
    ``Arc-Timestamp``.
  - ``dispatch_once() -> int`` processes one claimed batch; ``start()`` / ``stop()`` run
    the poll loop as a background task.

* ``build_callback_dispatcher(log, *, signer, **same_kwargs) -> CallbackDispatcher |
  None`` returns ``None`` when ``signer`` is ``None`` (no work-events key: no dispatcher,
  nothing is ever sent unsigned, pending deliveries stay pending).

* The request: ``POST`` with ``follow_redirects=False`` and a 10 s timeout; body = the
  pointer envelope JSON with EXACTLY ``event_id, kind, occurred_at, agent_did,
  workflow_id, run_id, node_id, task_id, correlation_id, status, classification, links``
  (no result, payload or callback_url); headers ``Arc-Event-Id``, ``Arc-Timestamp``,
  ``Arc-Key-Id`` (= ``signer.key_id``) and ``Arc-Signature: <algorithm>=<base64>`` over
  ``b"arc.work-event.v1\\n" + timestamp + b"\\n" + event_id + b"\\n" +
  sha256(body).hexdigest()`` (all ASCII; timestamp is the header value verbatim).

* Retry: any non-2xx (including 3xx), timeout or transport error settles ``retry`` with
  ``retry_after_s = min(30 * 2 ** (attempts - 1), 3600)`` (30, 60, 120, ...); the 10th
  failed attempt settles ``dead``, audits ``work_event.callback`` outcome ``dead`` and
  notifies the operator. An event whose classification is above the entry clearance is
  settled ``withheld`` with no request.

Doubles sit only at the edges: the receiver (``httpx.MockTransport``), DNS (injected
resolver), the clock, the operator notifier and the audit sink. The log is the real
``WorkEventLog`` over the in-memory arcstore backend (a subclass records each settle).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from arcstore.backends.memory import FakeBackend
from arcstore.work_events import WorkEvent, WorkEventLog
from arctrust import InProcessSigner, verify_signature
from arctrust.audit import AuditEvent
from arctrust.classification import Classification

HOST = "hooks.example.test"
URL = f"https://{HOST}/arc/callback"
PUBLIC_IP = "93.184.216.34"
AGENT = "did:arc:default:executor/aaaaaaaa"
DOMAIN = b"arc.work-event.v1"
ENVELOPE_KEYS = {
    "event_id",
    "kind",
    "occurred_at",
    "agent_did",
    "workflow_id",
    "run_id",
    "node_id",
    "task_id",
    "correlation_id",
    "status",
    "classification",
    "links",
}
BACKOFF = [min(30 * 2**n, 3600) for n in range(9)]  # 30, 60, 120, ... 3600


# --- edge doubles ---------------------------------------------------------------------


class Clock:
    """Settable, timezone-aware clock shared by the log and the dispatcher."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class WorkEventsSigner:
    """A COMP-019-shaped capability over a fixed test key (the real one is custody-held)."""

    def __init__(self, seed: bytes) -> None:
        self._signer = InProcessSigner(seed)

    @property
    def public_key(self) -> bytes:
        return self._signer.public_key

    @property
    def algorithm(self) -> str:
        return self._signer.algorithm

    @property
    def key_id(self) -> str:
        return hashlib.sha256(self.public_key).hexdigest()[:16]

    def sign(self, message: bytes) -> bytes:
        return self._signer.sign(message)


@dataclass(frozen=True)
class Entry:
    """An active allowlist entry (the fields COMP-013 reads from COMP-018)."""

    origin: str
    allow_private: bool = False
    clearance: Classification = Classification.UNCLASSIFIED
    status: str = "active"


@dataclass
class Policy:
    """Allowlist check double: the origin is allowlisted; records each check."""

    entry: Entry
    checked: list[str] = field(default_factory=list)

    def check(self, url: str) -> Entry:
        self.checked.append(url)
        return self.entry


@dataclass
class Resolver:
    """DNS double: answers from ``answers`` in order, repeating the last one."""

    answers: list[list[str]]
    calls: list[str] = field(default_factory=list)

    def __call__(self, hostname: str) -> Iterable[str]:
        self.calls.append(hostname)
        index = min(len(self.calls) - 1, len(self.answers) - 1)
        return list(self.answers[index])


@dataclass
class Receiver:
    """The app's callback endpoint: records every request; replies as configured."""

    reply: Callable[[httpx.Request], httpx.Response] = lambda _r: httpx.Response(204)
    requests: list[httpx.Request] = field(default_factory=list)
    arrived: asyncio.Event = field(default_factory=asyncio.Event)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.arrived.set()
        return self.reply(request)


@dataclass
class Notifier:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)

    async def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))


@dataclass
class AuditRecorder:
    events: list[AuditEvent] = field(default_factory=list)

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def callbacks(self, event_id: str) -> list[AuditEvent]:
        return [
            e for e in self.events if e.action == "work_event.callback" and event_id in e.target
        ]


class SpyLog(WorkEventLog):
    """The real log; every settle is recorded before it is applied."""

    def __init__(self, backend: Any, *, clock: Clock) -> None:
        super().__init__(backend, clock=clock)
        self.settled: list[dict[str, Any]] = []

    async def settle(
        self,
        event_id: str,
        *,
        outcome: Any,
        retry_after_s: float | None,
        error: str | None,
        consumer: str | None = None,
    ) -> bool:
        self.settled.append(
            {"event_id": event_id, "outcome": outcome, "retry_after_s": retry_after_s}
        )
        return await super().settle(
            event_id, outcome=outcome, retry_after_s=retry_after_s, error=error, consumer=consumer
        )


# --- rig ----------------------------------------------------------------------------------


@dataclass
class Rig:
    log: SpyLog
    clock: Clock
    signer: WorkEventsSigner
    operator: InProcessSigner
    policy: Policy
    resolver: Resolver
    receiver: Receiver
    notifier: Notifier
    audit: AuditRecorder

    def kwargs(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "transport": httpx.MockTransport(self.receiver),
            "resolver": self.resolver,
            "notify_operator": self.notifier,
            "audit_sink": self.audit,
            "clock": self.clock,
        }

    def dispatcher(self, **overrides: Any) -> Any:
        from arcui.work_events.dispatcher import CallbackDispatcher

        options = {**self.kwargs(), **overrides}
        return CallbackDispatcher(self.log, signer=self.signer, **options)

    async def append(self, **fields: Any) -> str:
        event_id = f"evt-{uuid.uuid4().hex}"
        payload: dict[str, Any] = {
            "event_id": event_id,
            "kind": "run.finished",
            "occurred_at": "2026-10-10T12:00:00+00:00",
            "agent_did": AGENT,
            "workflow_id": None,
            "run_id": f"run-{uuid.uuid4().hex}",
            "node_id": None,
            "task_id": None,
            "correlation_id": "order-42",
            "status": "done",
            "classification": "UNCLASSIFIED",
            "callback_url": URL,
        }
        payload.update(fields)
        assert await self.log.append(WorkEvent(**payload))
        return event_id

    def outcomes(self, event_id: str) -> list[Any]:
        return [s["outcome"] for s in self.log.settled if s["event_id"] == event_id]


@pytest.fixture
async def rig() -> Rig:
    backend = FakeBackend()
    await backend.start()
    clock = Clock()
    return Rig(
        log=SpyLog(backend, clock=clock),
        clock=clock,
        signer=WorkEventsSigner(bytes(range(32))),
        operator=InProcessSigner(bytes(range(32, 64))),
        policy=Policy(Entry(origin=f"https://{HOST}")),
        resolver=Resolver([[PUBLIC_IP]]),
        receiver=Receiver(),
        notifier=Notifier(),
        audit=AuditRecorder(),
    )


def _signed_message(request: httpx.Request) -> bytes:
    return b"\n".join(
        (
            DOMAIN,
            request.headers["Arc-Timestamp"].encode("ascii"),
            request.headers["Arc-Event-Id"].encode("ascii"),
            hashlib.sha256(request.content).hexdigest().encode("ascii"),
        )
    )


def _signature(request: httpx.Request) -> tuple[str, bytes]:
    algorithm, _, encoded = request.headers["Arc-Signature"].partition("=")
    return algorithm, base64.b64decode(encoded, validate=True)


# --- a signed, pinned, pointer-only delivery ---------------------------------------


class TestSignedDelivery:
    async def test_signature_verifies_with_the_work_events_key_and_key_id(self, rig: Rig) -> None:
        event_id = await rig.append()

        await rig.dispatcher().dispatch_once()

        (request,) = rig.receiver.requests
        assert request.method == "POST"
        assert request.headers["Arc-Event-Id"] == event_id
        assert request.headers["Arc-Timestamp"]
        assert request.headers["Arc-Key-Id"] == rig.signer.key_id
        algorithm, signature = _signature(request)
        assert algorithm == rig.signer.algorithm == "ed25519"
        assert verify_signature(
            algorithm, _signed_message(request), signature, rig.signer.public_key
        )
        assert rig.outcomes(event_id) == ["delivered"]
        (audited,) = rig.audit.callbacks(event_id)
        assert audited.outcome == "delivered"

    async def test_callback_is_not_signed_with_the_operator_key(self, rig: Rig) -> None:
        await rig.append()

        await rig.dispatcher().dispatch_once()

        (request,) = rig.receiver.requests
        algorithm, signature = _signature(request)
        message = _signed_message(request)
        assert not verify_signature(algorithm, message, signature, rig.operator.public_key)
        forged = rig.operator.sign(message)
        assert not verify_signature(algorithm, message, forged, rig.signer.public_key), (
            "an operator-key signature must not verify against the published work-events key"
        )

    async def test_tampered_body_fails_verification(self, rig: Rig) -> None:
        await rig.append()

        await rig.dispatcher().dispatch_once()

        (request,) = rig.receiver.requests
        algorithm, signature = _signature(request)
        body = json.loads(request.content)
        body["status"] = "failed"
        tampered = json.dumps(body).encode()
        message = b"\n".join(
            (
                DOMAIN,
                request.headers["Arc-Timestamp"].encode("ascii"),
                request.headers["Arc-Event-Id"].encode("ascii"),
                hashlib.sha256(tampered).hexdigest().encode("ascii"),
            )
        )
        assert not verify_signature(algorithm, message, signature, rig.signer.public_key)

    async def test_body_is_the_pointer_envelope_only(self, rig: Rig) -> None:
        event_id = await rig.append(task_id="intake-run-1")

        await rig.dispatcher().dispatch_once()

        (request,) = rig.receiver.requests
        body = json.loads(request.content)
        assert set(body) == ENVELOPE_KEYS
        assert body["event_id"] == event_id
        assert body["kind"] == "run.finished"
        assert body["status"] == "done"
        assert body["agent_did"] == AGENT
        assert isinstance(body["links"], dict)
        assert "callback_url" not in body and "result" not in body and "payload" not in body

    async def test_connects_to_the_checked_address_with_the_original_host(self, rig: Rig) -> None:
        await rig.append()

        await rig.dispatcher().dispatch_once()

        (request,) = rig.receiver.requests
        assert request.url.host == PUBLIC_IP
        assert request.url.path == "/arc/callback"
        assert request.headers["Host"] == HOST
        assert request.extensions.get("sni_hostname") == HOST
        timeout = request.extensions.get("timeout") or {}
        assert timeout and all(v is not None and v <= 10 for v in timeout.values()), timeout
        assert rig.policy.checked == [URL], "the allowlist is re-checked before delivery"


# --- no key, no dispatcher ----------------------------------------------------------


async def test_no_work_events_signer_means_no_dispatcher_and_no_post(rig: Rig) -> None:
    from arcui.work_events.dispatcher import build_callback_dispatcher

    event_id = await rig.append()

    dispatcher = build_callback_dispatcher(rig.log, signer=None, **rig.kwargs())

    assert dispatcher is None
    assert rig.receiver.requests == []
    claimed = await rig.log.claim_deliveries("probe", 10)
    assert [d.event_id for d in claimed] == [event_id], "the delivery must stay pending"


async def test_factory_with_a_signer_builds_a_dispatcher_that_delivers(rig: Rig) -> None:
    from arcui.work_events.dispatcher import build_callback_dispatcher

    await rig.append()

    dispatcher = build_callback_dispatcher(rig.log, signer=rig.signer, **rig.kwargs())

    assert dispatcher is not None
    await dispatcher.dispatch_once()
    assert len(rig.receiver.requests) == 1


async def test_start_and_stop_run_the_poll_loop(rig: Rig) -> None:
    await rig.append()
    dispatcher = rig.dispatcher(poll_interval_seconds=0.01)

    await dispatcher.start()
    try:
        await asyncio.wait_for(rig.receiver.arrived.wait(), timeout=5)
    finally:
        await dispatcher.stop()

    assert len(rig.receiver.requests) == 1


# --- retry, backoff and dead letter -------------------------------------------------


async def test_server_error_retries_with_backoff_30_60_120(rig: Rig) -> None:
    rig.receiver.reply = lambda _r: httpx.Response(500)
    event_id = await rig.append()
    dispatcher = rig.dispatcher()

    await dispatcher.dispatch_once()
    for delay in (30, 60):
        rig.clock.advance(delay - 1)
        await dispatcher.dispatch_once()
        rig.clock.advance(1)
        await dispatcher.dispatch_once()

    assert len(rig.receiver.requests) == 3, "retried before the backoff elapsed"
    retries = [s for s in rig.log.settled if s["event_id"] == event_id]
    assert [s["outcome"] for s in retries] == ["retry", "retry", "retry"]
    assert [s["retry_after_s"] for s in retries] == [30, 60, 120]
    assert all(e.outcome == "retry" for e in rig.audit.callbacks(event_id))
    assert len(rig.audit.callbacks(event_id)) == 3


async def test_timeout_is_a_failed_attempt(rig: Rig) -> None:
    def _timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("receiver too slow", request=request)

    rig.receiver.reply = _timeout
    event_id = await rig.append()

    await rig.dispatcher().dispatch_once()

    assert rig.outcomes(event_id) == ["retry"]
    assert rig.log.settled[-1]["retry_after_s"] == 30


async def test_tenth_failure_marks_dead_audits_and_notifies_the_operator(rig: Rig) -> None:
    rig.receiver.reply = lambda _r: httpx.Response(503)
    event_id = await rig.append()
    dispatcher = rig.dispatcher()

    await dispatcher.dispatch_once()
    for delay in BACKOFF:
        rig.clock.advance(delay)
        await dispatcher.dispatch_once()
    rig.clock.advance(7200)
    await dispatcher.dispatch_once()  # a dead delivery is never claimed again

    assert len(rig.receiver.requests) == 10
    assert rig.outcomes(event_id) == ["retry"] * 9 + ["dead"]
    assert [s["retry_after_s"] for s in rig.log.settled[:9]] == BACKOFF
    audited = rig.audit.callbacks(event_id)
    assert audited[-1].outcome == "dead"
    assert len(audited) == 10
    assert len(rig.notifier.calls) == 1
    args, kwargs = rig.notifier.calls[0]
    assert event_id in [*args, *kwargs.values()]


async def test_redirect_is_not_followed_and_counts_as_a_failure(rig: Rig) -> None:
    rig.receiver.reply = lambda _r: httpx.Response(
        302, headers={"Location": "http://169.254.169.254/latest/meta-data/"}
    )
    event_id = await rig.append()

    await rig.dispatcher().dispatch_once()

    assert len(rig.receiver.requests) == 1, "the redirect was followed"
    assert rig.outcomes(event_id) == ["retry"]


# --- SSRF at delivery time -----------------------------------------------------------


async def test_dns_rebinding_after_the_check_never_reaches_loopback(rig: Rig) -> None:
    """The host answers public once, then 127.0.0.1: only the checked address is used."""
    rig.resolver.answers = [[PUBLIC_IP], ["127.0.0.1"]]
    await rig.append()

    await rig.dispatcher().dispatch_once()

    assert rig.receiver.requests, "the checked public address should have been delivered to"
    for request in rig.receiver.requests:
        assert request.url.host == PUBLIC_IP, "the transport was handed a name to re-resolve"
        assert request.url.host != "127.0.0.1"


async def test_host_now_resolving_to_loopback_is_refused_with_no_post(rig: Rig) -> None:
    """Allowlisted at intake, rebound to 127.0.0.1 by delivery time: refused."""
    rig.resolver.answers = [["127.0.0.1"]]
    event_id = await rig.append()

    await rig.dispatcher().dispatch_once()

    assert rig.receiver.requests == []
    assert rig.outcomes(event_id) and "delivered" not in rig.outcomes(event_id)
    audited = rig.audit.callbacks(event_id)
    assert audited and all(e.outcome != "delivered" for e in audited)


async def test_mixed_answer_with_one_private_address_is_refused(rig: Rig) -> None:
    rig.resolver.answers = [[PUBLIC_IP, "10.0.0.5"]]
    event_id = await rig.append()

    await rig.dispatcher().dispatch_once()

    assert rig.receiver.requests == []
    assert "delivered" not in rig.outcomes(event_id)


async def test_allow_private_entry_delivers_to_the_checked_private_address(rig: Rig) -> None:
    rig.policy.entry = Entry(origin=f"https://{HOST}", allow_private=True)
    rig.resolver.answers = [["10.0.0.5"]]
    event_id = await rig.append()

    await rig.dispatcher().dispatch_once()

    (request,) = rig.receiver.requests
    assert request.url.host == "10.0.0.5"
    assert request.headers["Host"] == HOST
    assert rig.outcomes(event_id) == ["delivered"]


async def test_metadata_address_refused_even_with_allow_private(rig: Rig) -> None:
    rig.policy.entry = Entry(origin=f"https://{HOST}", allow_private=True)
    rig.resolver.answers = [["169.254.169.254"]]
    event_id = await rig.append()

    await rig.dispatcher().dispatch_once()

    assert rig.receiver.requests == []
    assert "delivered" not in rig.outcomes(event_id)


async def test_policy_refusal_at_delivery_sends_nothing(rig: Rig) -> None:
    """An origin removed from the allowlist after intake is never posted to."""

    class Removed:
        def check(self, url: str) -> Entry:
            raise ValueError("callback_not_allowed")

    event_id = await rig.append()

    await rig.dispatcher(policy=Removed()).dispatch_once()

    assert rig.receiver.requests == []
    assert "delivered" not in rig.outcomes(event_id)


# --- classification ------------------------------------------------------------------


async def test_secret_event_to_unclassified_entry_is_withheld(rig: Rig) -> None:
    event_id = await rig.append(classification="SECRET")

    await rig.dispatcher().dispatch_once()

    assert rig.receiver.requests == []
    assert rig.outcomes(event_id) == ["withheld"]
    (audited,) = rig.audit.callbacks(event_id)
    assert audited.outcome == "withheld"
    rig.clock.advance(7200)
    await rig.dispatcher().dispatch_once()
    assert rig.receiver.requests == [], "a withheld delivery is never retried"


async def test_event_at_entry_clearance_is_delivered(rig: Rig) -> None:
    rig.policy.entry = Entry(origin=f"https://{HOST}", clearance=Classification.SECRET)
    event_id = await rig.append(classification="SECRET")

    await rig.dispatcher().dispatch_once()

    assert len(rig.receiver.requests) == 1
    assert rig.outcomes(event_id) == ["delivered"]
