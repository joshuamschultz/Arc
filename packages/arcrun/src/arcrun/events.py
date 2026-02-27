"""Event system with SHA-256 hash chain for tamper-evident audit trails."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

logger = logging.getLogger(__name__)

GENESIS_PREV_HASH = "0" * 64


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
    data: MappingProxyType  # type: ignore[type-arg]  # Generic arg omitted: MappingProxyType[str, Any] unsupported
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
    """Emits events, collects them, optionally calls handler. Thread-safe."""

    def __init__(self, run_id: str, on_event: Callable[[Event], None] | None = None) -> None:
        self._run_id = run_id
        self._on_event = on_event
        self._events: list[Event] = []
        self._lock = threading.Lock()

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
                self._on_event(event)
            except Exception:
                logger.warning("Observer callback failed", exc_info=True)
        return event

    @property
    def events(self) -> list[Event]:
        with self._lock:
            return list(self._events)
