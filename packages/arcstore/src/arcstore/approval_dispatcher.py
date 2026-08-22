"""Durable approval notification dispatcher.

The dispatcher owns only the outbox worker seam.  It has no knowledge of ArcUI
or a transport: an injected sink receives a deliberately small typed event,
while the ArcStore backend owns leasing, retry visibility, and durability.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from arctrust.audit import AuditEvent, AuditSink, emit
from pydantic import BaseModel, ConfigDict, Field, ValidationError

ApprovalStatus = Literal["pending", "approved", "denied", "expired"]
_logger = logging.getLogger("arcstore.approval_dispatcher")


class ApprovalNotification(BaseModel):
    """Safe notification projection; raw event payloads never reach a sink."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(min_length=1, max_length=512)
    approval_id: str = Field(min_length=1, max_length=512)
    status: ApprovalStatus
    agent_did: str | None = Field(default=None, max_length=512)
    tool: str | None = Field(default=None, max_length=512)
    classification: str = Field(default="UNCLASSIFIED", min_length=1, max_length=64)
    attempts: int = Field(ge=1, le=1_000_000)


class ApprovalNotificationSink(Protocol):
    """Typed, idempotent sink keyed by ``notification.event_id``.

    Delivery is intentionally at-least-once: a process crash after the sink
    accepts an event and before the database acknowledgement can redeliver it.
    Implementations must durably deduplicate by ``event_id``.
    """

    async def __call__(self, notification: ApprovalNotification) -> None: ...


class ApprovalOutboxBackend(Protocol):
    """The minimum backend seam required by the dispatcher."""

    async def claim_outbox(self, consumer_id: str, *, limit: int) -> list[dict[str, Any]]: ...

    async def ack_outbox(self, consumer_id: str, event_ids: list[str]) -> None: ...

    async def nack_outbox(
        self,
        consumer_id: str,
        event_id: str,
        *,
        retry_after_seconds: float,
    ) -> bool: ...

    async def reject_outbox(self, consumer_id: str, event_id: str) -> bool: ...


@dataclass(frozen=True)
class ApprovalDispatcherConfig:
    """Bounded polling and deterministic exponential retry policy."""

    worker_id: str
    batch_size: int = 100
    poll_interval_seconds: float = 1.0
    retry_base_seconds: float = 1.0
    retry_max_seconds: float = 300.0

    def __post_init__(self) -> None:
        if not self.worker_id:
            raise ValueError("worker_id must not be empty")
        if not 1 <= self.batch_size <= 1000:
            raise ValueError("batch_size must be between 1 and 1000")
        if self.poll_interval_seconds < 0:
            raise ValueError("poll interval must not be negative")
        if self.retry_base_seconds < 0:
            raise ValueError("retry base must not be negative")
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("retry max must be at least retry base")


def _safe_audit(sink: AuditSink | None, *, action: str, target: str, outcome: str) -> None:
    if sink is None:
        return
    try:
        emit(
            AuditEvent(
                actor_did="did:arc:system:approval-dispatcher",
                action=action,
                target=target[:512],
                outcome=outcome,
            ),
            sink,
        )
    except Exception:  # audit is fail-open and must not affect delivery state
        _logger.warning("approval dispatcher audit failed", exc_info=True)


def _notification(row: Mapping[str, Any]) -> ApprovalNotification:
    event = row.get("event")
    if not isinstance(event, Mapping):
        raise ValueError("outbox event must be an object")
    event_id = row.get("event_id")
    approval_id = row.get("approval_id")
    if not isinstance(event_id, str) or not isinstance(approval_id, str):
        raise ValueError("outbox identity fields must be strings")
    embedded_id = event.get("approval_id")
    if embedded_id is not None and embedded_id != approval_id:
        raise ValueError("outbox approval identity mismatch")
    return ApprovalNotification(
        event_id=event_id,
        approval_id=approval_id,
        status=event.get("status", "pending"),
        agent_did=event.get("agent_did"),
        tool=event.get("tool"),
        classification=event.get("classification", "UNCLASSIFIED"),
        attempts=row.get("attempts", 1),
    )


class ApprovalNotificationDispatcher:
    """Lease, deliver, and acknowledge approval notifications durably."""

    def __init__(
        self,
        backend: ApprovalOutboxBackend,
        sink: ApprovalNotificationSink,
        config: ApprovalDispatcherConfig,
        *,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._backend = backend
        self._sink = sink
        self._config = config
        self._audit_sink = audit_sink
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def dispatch_once(self) -> int:
        """Deliver at most one bounded batch and return claimed item count."""
        rows = await self._backend.claim_outbox(
            self._config.worker_id,
            limit=self._config.batch_size,
        )
        acknowledged: list[str] = []
        for row in rows:
            event_id = row.get("event_id")
            if not isinstance(event_id, str):
                await self._reject(row, "", "invalid_event_id")
                continue
            if not event_id:
                await self._reject(row, event_id, "invalid_event_id")
                continue
            try:
                notification = _notification(row)
            except (ValidationError, TypeError, ValueError) as exc:
                await self._reject(row, event_id, "invalid_notification")
                _logger.warning("quarantined approval notification %s: %s", event_id, exc)
                continue
            try:
                await self._sink(notification)
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._nack(row, event_id, "sink_failure")
                _logger.exception("approval notification sink failed for %s", event_id)
                continue
            acknowledged.append(event_id)
            _safe_audit(
                self._audit_sink,
                action="approval.notification.delivered",
                target=notification.approval_id,
                outcome="delivered",
            )
        if acknowledged:
            try:
                await self._backend.ack_outbox(self._config.worker_id, acknowledged)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A lease will recover this batch. The idempotent sink contract
                # makes a later claim safe after a process crash.
                _logger.exception("approval notification ack failed")
                _safe_audit(
                    self._audit_sink,
                    action="approval.notification.ack_failed",
                    target="batch",
                    outcome="lease_recovery",
                )
        return len(rows)

    async def _reject(self, row: Mapping[str, Any], event_id: str, outcome: str) -> None:
        """Durably remove a malformed claimed row from the ready queue."""
        del row  # the backend owns the complete durable row and its lease
        try:
            rejected = await self._backend.reject_outbox(self._config.worker_id, event_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("approval notification reject failed")
            rejected = False
        _safe_audit(
            self._audit_sink,
            action="approval.notification.rejected",
            target=event_id or "unknown",
            outcome=outcome if rejected else "lease_recovery",
        )

    async def _nack(self, row: Mapping[str, Any], event_id: str, outcome: str) -> None:
        attempts = row.get("attempts", 1)
        attempts = attempts if isinstance(attempts, int) and attempts > 0 else 1
        delay = min(
            self._config.retry_max_seconds,
            self._config.retry_base_seconds * (2 ** (attempts - 1)),
        )
        try:
            changed = await self._backend.nack_outbox(
                self._config.worker_id,
                event_id,
                retry_after_seconds=delay,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("approval notification nack failed for %s", event_id)
            changed = False
        _safe_audit(
            self._audit_sink,
            action="approval.notification.retry",
            target=event_id,
            outcome=outcome if changed else "lease_lost",
        )

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        """Poll until stopped; cancellation exits without swallowing cancellation."""
        external_stop = stop_event
        try:
            while not self._stop.is_set() and not (external_stop and external_stop.is_set()):
                processed = await self.dispatch_once()
                if processed:
                    continue
                await self._wait_for_stop(external_stop)
        finally:
            self._stop.set()

    async def _wait_for_stop(self, external_stop: asyncio.Event | None) -> None:
        waits = [asyncio.create_task(self._stop.wait())]
        if external_stop is not None:
            waits.append(asyncio.create_task(external_stop.wait()))
        try:
            done, _ = await asyncio.wait(
                waits,
                timeout=self._config.poll_interval_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            del done
        finally:
            for wait in waits:
                if not wait.done():
                    wait.cancel()
            await asyncio.gather(*waits, return_exceptions=True)

    async def start(self) -> None:
        """Start one worker task; repeated starts are idempotent."""
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self.run(), name="arcstore-approval-dispatcher")

    async def stop(self) -> None:
        """Stop the worker promptly, returning an interrupted lease for retry."""
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise


__all__ = [
    "ApprovalDispatcherConfig",
    "ApprovalNotification",
    "ApprovalNotificationDispatcher",
    "ApprovalNotificationSink",
    "ApprovalOutboxBackend",
]
