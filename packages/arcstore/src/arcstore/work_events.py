"""Ordered work-event log with durable callback deliveries (SPEC-088 COMP-012).

A ``WorkEvent`` is a small pointer record ("run X finished", "gate Y waits")
that an app reads in ``seq`` order or receives at its callback URL. The log
owns no transport: it stores events once per ``event_id``, pages them by the
store-assigned ``seq``, and leases due deliveries to a dispatcher, the same
outbox shape as :mod:`arcstore.approval_dispatcher`.

The clock is injected and passed to the backend as ``now`` so leases and retry
delays are deterministic on every backend.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from arctrust.audit import AuditEvent, AuditSink, emit
from pydantic import BaseModel, ConfigDict, Field, field_validator

from arcstore.backends.base import WORK_EVENT_FILTER_FIELDS

WorkEventKind = Literal[
    "run.finished", "run.failed", "gate.waiting", "human_input.waiting", "gate.escalated"
]
DeliveryOutcome = Literal["delivered", "retry", "withheld", "dead"]

#: Largest page one ``read`` or ``claim_deliveries`` call may ask for.
MAX_PAGE = 1000
_ACTOR_DID = "did:arc:system:work-events"
_logger = logging.getLogger("arcstore.work_events")


class WorkEvent(BaseModel):
    """One closed, immutable work-event envelope; ``seq`` is assigned by the store."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(min_length=1, max_length=512)
    seq: int | None = None
    kind: WorkEventKind
    occurred_at: str
    agent_did: str = Field(min_length=1, max_length=512)
    workflow_id: str | None = Field(default=None, max_length=512)
    run_id: str | None = Field(default=None, max_length=512)
    node_id: str | None = Field(default=None, max_length=512)
    task_id: str | None = Field(default=None, max_length=512)
    correlation_id: str | None = Field(default=None, max_length=512)
    status: str = Field(min_length=1, max_length=64)
    classification: str = Field(min_length=1, max_length=64)
    callback_url: str | None = Field(default=None, max_length=2048)

    @field_validator("occurred_at")
    @classmethod
    def _aware_occurred_at(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("occurred_at must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value


class WorkEventDelivery(BaseModel):
    """A leased callback delivery handed to one consumer."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    event_id: str
    callback_url: str
    attempts: int = Field(ge=1)


class WorkEventBackend(Protocol):
    """The backend seam ``WorkEventLog`` needs (see ``ArcStoreBackend``)."""

    async def append_work_event(
        self, event: dict[str, Any], *, callback_url: str | None, now: datetime
    ) -> bool: ...

    async def read_work_events(
        self,
        after_seq: int,
        *,
        limit: int,
        where: Mapping[str, str | None] | None = None,
    ) -> list[dict[str, Any]]: ...

    async def claim_work_event_deliveries(
        self, consumer_id: str, *, limit: int, now: datetime, lease_seconds: float
    ) -> list[dict[str, Any]]: ...

    async def settle_work_event_delivery(
        self,
        event_id: str,
        *,
        state: str,
        next_attempt_at: datetime | None,
        error: str | None,
        consumer_id: str | None,
        now: datetime,
    ) -> bool: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


class WorkEventLog:
    """Append, page and deliver work events over an ArcStore backend."""

    def __init__(
        self,
        backend: WorkEventBackend,
        *,
        clock: Callable[[], datetime] | None = None,
        lease_seconds: float = 60.0,
        sink: AuditSink | None = None,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._backend = backend
        self._clock = clock or _utc_now
        self._lease_seconds = lease_seconds
        self._sink = sink

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("work-event clock must return a timezone-aware datetime")
        return now

    async def append(self, event: WorkEvent) -> bool:
        """Store ``event`` once per ``event_id``; ``True`` only for the first append.

        A caller-supplied ``seq`` is ignored: the order is the store's. An
        event with ``callback_url`` gets exactly one ``pending`` delivery in
        the same write.
        """
        envelope = event.model_dump(mode="json", exclude={"seq"})
        inserted = await self._backend.append_work_event(
            envelope, callback_url=event.callback_url, now=self._now()
        )
        if inserted:
            self._audit("work_event.append", event.event_id, event.kind)
        return inserted

    async def read(
        self,
        after_seq: int,
        *,
        limit: int,
        where: Mapping[str, str | None] | None = None,
    ) -> list[WorkEvent]:
        """Events with ``seq > after_seq`` in strictly increasing ``seq`` order.

        ``where`` is field equality over the stored columns (for example
        ``{"agent_did": ...}``); an unknown field is refused.
        """
        if after_seq < 0:
            raise ValueError("after_seq must not be negative")
        _check_page(limit)
        unknown = set(where or {}) - WORK_EVENT_FILTER_FIELDS
        if unknown:
            raise ValueError(f"unsupported work-event filter: {sorted(unknown)}")
        rows = await self._backend.read_work_events(after_seq, limit=limit, where=where)
        return [WorkEvent.model_validate(row) for row in rows]

    async def claim_deliveries(self, consumer: str, limit: int) -> list[WorkEventDelivery]:
        """Lease up to ``limit`` due deliveries to ``consumer`` for ``lease_seconds``.

        A leased delivery is hidden from other consumers until it is settled
        or its lease expires (a crashed worker never strands a delivery).
        """
        if not consumer:
            raise ValueError("consumer must not be empty")
        _check_page(limit)
        rows = await self._backend.claim_work_event_deliveries(
            consumer, limit=limit, now=self._now(), lease_seconds=self._lease_seconds
        )
        return [WorkEventDelivery.model_validate(row) for row in rows]

    async def settle(
        self,
        event_id: str,
        *,
        outcome: DeliveryOutcome,
        retry_after_s: float | None,
        error: str | None,
        consumer: str | None = None,
    ) -> bool:
        """Record a claimed delivery's outcome; return whether it was applied.

        ``retry`` returns the delivery to ``pending``, hidden until
        ``now + retry_after_s``; ``delivered``, ``withheld`` and ``dead`` end
        it. Pass ``consumer`` to settle only that consumer's live lease, so a
        worker whose lease expired cannot overwrite the new owner's result.
        """
        now = self._now()
        next_attempt_at: datetime | None = None
        state: str = outcome
        if outcome == "retry":
            if retry_after_s is None or retry_after_s < 0:
                raise ValueError("retry needs a non-negative retry_after_s")
            next_attempt_at = now + timedelta(seconds=retry_after_s)
            state = "pending"
        settled = await self._backend.settle_work_event_delivery(
            event_id,
            state=state,
            next_attempt_at=next_attempt_at,
            error=error[:1024] if error else error,
            consumer_id=consumer,
            now=now,
        )
        self._audit("work_event.delivery.settle", event_id, outcome if settled else "no-op")
        return settled

    def _audit(self, action: str, target: str, outcome: str) -> None:
        if self._sink is None:
            return
        try:
            emit(
                AuditEvent(
                    actor_did=_ACTOR_DID, action=action, target=target[:512], outcome=outcome
                ),
                self._sink,
            )
        except Exception:  # audit is fail-open and must not change delivery state
            _logger.warning("work-event audit failed", exc_info=True)


def _check_page(limit: int) -> None:
    if not 1 <= limit <= MAX_PAGE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE}")


__all__ = [
    "MAX_PAGE",
    "DeliveryOutcome",
    "WorkEvent",
    "WorkEventBackend",
    "WorkEventDelivery",
    "WorkEventKind",
    "WorkEventLog",
]
