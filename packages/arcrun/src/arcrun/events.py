"""Event system with SHA-256 hash chain for tamper-evident audit trails."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from arcstore.records import SpoolRecord as _SpoolRecord
from arcstore.spool import record as _spool_record

logger = logging.getLogger(__name__)

GENESIS_PREV_HASH = "0" * 64

# Loop-lifecycle event types mirrored to the arcstore operational spool
# (SPEC-026 FR-4: run start / step / finish). Other events (tool.*) stay in the
# in-memory hash chain only — the spool captures run lifecycle, not every tick.
_RUN_EVENT_TYPES = frozenset(
    {"strategy.selected", "turn.start", "turn.end", "loop.completed"}
)


def _canonical_bytes(
    event_type: str,
    timestamp: float,
    run_id: str,
    data: Mapping[str, Any],
    sequence: int,
) -> bytes:
    """Deterministic serialization for hash computation."""
    return json.dumps(
        {
            "type": event_type,
            "timestamp": timestamp,
            "run_id": run_id,
            "data": dict(data),
            "sequence": sequence,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _compute_event_hash(prev_hash: str, canonical: bytes) -> str:
    """SHA-256 of prev_hash + canonical event bytes."""
    return hashlib.sha256(prev_hash.encode("ascii") + canonical).hexdigest()


@dataclass(frozen=True)
class Event:
    """Immutable event with hash chain fields."""

    type: str
    timestamp: float
    run_id: str
    data: MappingProxyType  # type: ignore[type-arg]  # reason: Generic arg omitted: MappingProxyType[str, Any] unsupported
    sequence: int = 0
    prev_hash: str = ""
    event_hash: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.data, dict):
            object.__setattr__(self, "data", MappingProxyType(self.data))


@dataclass
class ChainVerificationResult:
    """Result of verify_chain()."""

    valid: bool
    event_count: int
    first_broken_index: int | None = None
    error: str | None = None


def verify_chain(events: list[Event]) -> ChainVerificationResult:
    """Verify integrity of an event chain."""
    for i, event in enumerate(events):
        canonical = _canonical_bytes(
            event.type, event.timestamp, event.run_id, event.data, event.sequence
        )
        expected = _compute_event_hash(event.prev_hash, canonical)
        if expected != event.event_hash:
            return ChainVerificationResult(False, len(events), i, "self-hash mismatch")
        expected_prev = events[i - 1].event_hash if i > 0 else GENESIS_PREV_HASH
        if event.prev_hash != expected_prev:
            return ChainVerificationResult(False, len(events), i, "chain break")
        if event.sequence != i:
            return ChainVerificationResult(False, len(events), i, "sequence gap")
    return ChainVerificationResult(True, len(events))


class EventBus:
    """Emits events, collects them, optionally calls handler. Thread-safe.

    Observers may be sync (``Callable[[Event], None]``) or async
    (``Callable[[Event], Awaitable[None]]``). Async observers are
    scheduled on the running event loop and tracked so the task isn't
    garbage-collected before completion. If an async observer is passed
    but ``emit`` is called from a sync context with no running loop, the
    returned coroutine is closed and a warning is logged — silently
    dropping the coroutine (Python's default) would emit a
    ``RuntimeWarning: coroutine was never awaited`` and lose all observer
    side effects (audit emits, UI HUD updates), which is an unforgiving
    failure mode.
    """

    def __init__(
        self,
        run_id: str,
        on_event: Callable[[Event], Awaitable[None] | None] | None = None,
        *,
        spool_actor_did: str | None = None,
    ) -> None:
        self._run_id = run_id
        self._on_event = on_event
        # When set, loop-lifecycle events are mirrored to the arcstore spool
        # under this DID (SPEC-026 FR-4). None disables operational recording.
        self._spool_actor_did = spool_actor_did
        self._events: list[Event] = []
        self._lock = threading.Lock()
        # Strong refs to pending async-observer tasks so the loop doesn't
        # GC them mid-flight. Tasks self-remove via done_callback.
        self._pending_observer_tasks: set[asyncio.Task[Any]] = set()

    def emit(self, event_type: str, data: dict[str, Any] | None = None) -> Event:
        """Create event with hash chain, append to log, call handler if set."""
        with self._lock:
            sequence = len(self._events)
            prev_hash = self._events[-1].event_hash if self._events else GENESIS_PREV_HASH
            timestamp = time.time()
            frozen_data = MappingProxyType(dict(data or {}))
            canonical = _canonical_bytes(event_type, timestamp, self._run_id, frozen_data, sequence)
            event_hash = _compute_event_hash(prev_hash, canonical)
            event = Event(
                type=event_type,
                timestamp=timestamp,
                run_id=self._run_id,
                data=frozen_data,
                sequence=sequence,
                prev_hash=prev_hash,
                event_hash=event_hash,
            )
            self._events.append(event)
        # Observer callback OUTSIDE lock to prevent deadlock
        if self._on_event is not None:
            try:
                result = self._on_event(event)
                if inspect.iscoroutine(result):
                    self._schedule_async_observer(result)
            except Exception:  # reason: fail-open — log + continue
                logger.warning("Observer callback failed", exc_info=True)
        self._record_run_event(event)
        return event

    def _record_run_event(self, event: Event) -> None:
        """Mirror a loop-lifecycle event to the arcstore operational spool.

        Side-channel only (SPEC-026 FR-4): gated by an actor DID, scoped to
        lifecycle event types, and itself fail-open (``record()`` swallows IO
        errors) so it can never break the loop.
        """
        if self._spool_actor_did is None or event.type not in _RUN_EVENT_TYPES:
            return
        _spool_record(
            _SpoolRecord(
                kind="run_event",
                actor_did=self._spool_actor_did,
                request_id=self._run_id,
                name=event.type,
            )
        )

    def _schedule_async_observer(self, coro: Any) -> None:
        """Schedule an async observer's coroutine on the running loop.

        Holds a strong ref to the task until it finishes so it isn't
        GC'd before the side effects run. If no loop is running we
        close the coroutine and warn — leaving it un-awaited would
        drop the side effects and emit a confusing RuntimeWarning.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            logger.warning(
                "EventBus received an async on_event but no running loop is "
                "available; observer coroutine was discarded. Call emit() "
                "from an async context or pass a sync callable."
            )
            return
        task = loop.create_task(coro)
        self._pending_observer_tasks.add(task)
        task.add_done_callback(self._pending_observer_tasks.discard)

    @property
    def events(self) -> list[Event]:
        with self._lock:
            return list(self._events)
