"""Event system with SHA-256 hash chain for tamper-evident audit trails."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import secrets
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
# (SPEC-026 FR-4: run start / step / finish). ``loop.complete`` is the universal
# terminal — ``_build_result`` emits it on every loop exit, so it is the marker
# that tells observers a run finished (vs ``loop.completed``, which fires only on
# the structured-completion / max_turns / max_cost paths).
_RUN_EVENT_TYPES = frozenset(
    {"strategy.selected", "turn.start", "turn.end", "loop.complete", "loop.completed"}
)

# Tool-lifecycle event types mirrored as ``tool_event`` records (SPEC-028 FR-1).
# Code execution (``execute_python``) rides these like any other tool (FR-2).
_TOOL_EVENT_TYPES = frozenset({"tool.start", "tool.end", "tool.error"})


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
        store_raw_bodies: bool = False,
        sample_rate: float = 1.0,
    ) -> None:
        self._run_id = run_id
        self._on_event = on_event
        # When set, loop-lifecycle events are mirrored to the arcstore spool
        # under this DID (SPEC-026 FR-4). None disables operational recording.
        self._spool_actor_did = spool_actor_did
        # Raw-capture posture flows in from the caller (arcagent/arccli), never
        # read from config here — arcrun stays config-free (SPEC-028 NFR-4).
        self.store_raw_bodies = store_raw_bodies
        # Probabilistic thinning of high-frequency tool_events only; lifecycle
        # and errors are never sampled out (SPEC-028 NFR-5 / task 2.6).
        self._sample_rate = sample_rate
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
        """Mirror a lifecycle or tool event to the arcstore operational spool.

        Side-channel only (SPEC-026 FR-4 / SPEC-028 FR-1): gated by an actor DID
        and fully fail-open (NFR-3) — record *construction* as well as the IO is
        wrapped, so a malformed payload can never break the loop it observes.
        ``run_event`` lifecycle markers are always recorded; ``tool_event``s
        carry the executor-computed digests and may be sampled.
        """
        actor_did = self._spool_actor_did
        if actor_did is None:
            return
        try:
            if event.type in _RUN_EVENT_TYPES:
                _spool_record(
                    _SpoolRecord(
                        kind="run_event",
                        actor_did=actor_did,
                        request_id=self._run_id,
                        name=event.type,
                    )
                )
            elif event.type in _TOOL_EVENT_TYPES:
                self._record_tool_event(event, actor_did)
        except Exception as exc:  # reason: fail-open (NFR-3) — telemetry never breaks the run
            # Log the exception *type* only — never exc_info/message, which can echo
            # a record field value (e.g. a body under store_raw_bodies) into the log
            # (LLM02/LLM07). Type name is enough to debug a construction fault.
            logger.warning(
                "spool record failed for %s (%s) — swallowing (NFR-3)",
                event.type,
                type(exc).__name__,
            )

    def _record_tool_event(self, event: Event, actor_did: str) -> None:
        """Map a ``tool.*`` event to a ``tool_event`` spool record (SPEC-028 FR-1).

        Consumes the digest/size fields the executor computed at source (C1).
        Bodies (args, result/code) ride ``extra`` only when raw capture is on
        (NFR-2). Routine start/end may be sampled out; errors never are.
        """
        is_error = event.type == "tool.error"
        if not is_error and not self._should_sample():
            return
        data = event.data
        extra: dict[str, Any] = {}
        if self.store_raw_bodies:
            if "arguments" in data:
                extra["args"] = dict(data["arguments"])
            if "result" in data:
                extra["result"] = data["result"]
        # A tool's opaque self-annotation is small scalar signal (not a body), so
        # it is always kept — never gated behind raw-body capture.
        if isinstance(data.get("tool_extra"), dict):
            extra.update(data["tool_extra"])
        _spool_record(
            _SpoolRecord(
                kind="tool_event",
                actor_did=actor_did,
                request_id=self._run_id,
                tool_name=data.get("name"),
                phase=event.type.split(".", 1)[1],
                outcome="error" if is_error else "ok",
                latency_ms=data.get("duration_ms"),
                args_digest=data.get("args_digest"),
                args_size=data.get("args_size"),
                result_digest=data.get("result_digest"),
                result_size=data.get("result_size"),
                extra=extra,
            )
        )

    def _should_sample(self) -> bool:
        """Keep a routine tool_event with probability ``sample_rate``."""
        if self._sample_rate >= 1.0:
            return True
        if self._sample_rate <= 0.0:
            return False
        # secrets.randbelow gives a uniform draw without a separate PRNG import;
        # sampling here is not security-sensitive, but it avoids a global RNG.
        return secrets.randbelow(1_000_000) < int(self._sample_rate * 1_000_000)

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

    @property
    def spool_actor_did(self) -> str | None:
        """The DID this run spools under (None when operational recording is off).

        Read-only accessor so a layer above (arcagent spawn) can derive lineage
        without reaching into a private attribute.
        """
        return self._spool_actor_did
