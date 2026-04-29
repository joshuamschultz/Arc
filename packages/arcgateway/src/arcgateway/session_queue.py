"""QueueManager — per-session bounded FIFO queue with idle TTL eviction.

Extracts queue state and operations from SessionRouter so the router
core stays focused on race-guard and task-spawn logic.

Design:
    - Bounded depth: max 100 events per session (configurable).
    - Idle TTL eviction: sessions with no activity for 1 hour are
      evicted on the next cleanup_idle() call to prevent unbounded growth.
    - Thread safety: cooperatively asyncio-safe (all mutations synchronous
      between awaits, matching SessionRouter's design contract).
"""

from __future__ import annotations

import collections
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arcgateway.executor import InboundEvent

_logger = logging.getLogger("arcgateway.session_queue")

# Maximum number of queued events per session before new arrivals are dropped.
_MAX_QUEUE_DEPTH = 100

# Sessions with no activity for this many seconds are eligible for eviction.
_IDLE_TTL_SECONDS = 3600  # 1 hour


class QueueManager:
    """Manages per-session bounded FIFO queues with idle TTL eviction.

    Attributes:
        _queues:       Maps session_key → deque of InboundEvents.
        _last_active:  Maps session_key → unix timestamp of last activity.
        _max_depth:    Maximum queue depth per session.
        _idle_ttl:     Idle session lifetime in seconds.
    """

    def __init__(
        self,
        *,
        max_depth: int = _MAX_QUEUE_DEPTH,
        idle_ttl_seconds: float = _IDLE_TTL_SECONDS,
    ) -> None:
        """Initialise QueueManager.

        Args:
            max_depth:         Maximum events per session queue (bounded depth).
            idle_ttl_seconds:  Seconds of inactivity before a session is evicted.
        """
        self._queues: dict[str, collections.deque[InboundEvent]] = {}
        self._last_active: dict[str, float] = {}
        self._max_depth = max_depth
        self._idle_ttl = idle_ttl_seconds

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def enqueue(self, session_key: str, event: InboundEvent) -> bool:
        """Append an event to the named session queue.

        Initialises the queue on first use.  Drops the event and logs a
        warning if the queue is already at max depth (back-pressure).

        Args:
            session_key: Unique session identifier.
            event:       Event to enqueue.

        Returns:
            True if the event was enqueued; False if dropped (queue full).
        """
        self._ensure_session(session_key)

        q = self._queues[session_key]
        if len(q) >= self._max_depth:
            _logger.warning(
                "Session %s queue at max depth %d — dropping event",
                session_key,
                self._max_depth,
            )
            return False

        q.append(event)
        self._touch(session_key)
        _logger.debug(
            "Session %s queued event (depth=%d)",
            session_key,
            len(q),
        )
        return True

    def dequeue(self, session_key: str) -> InboundEvent | None:
        """Pop and return the next event from the named session queue.

        Returns None if the queue does not exist or is empty.

        Args:
            session_key: Unique session identifier.

        Returns:
            Next InboundEvent or None.
        """
        q = self._queues.get(session_key)
        if not q:
            return None
        self._touch(session_key)
        return q.popleft()

    def peek_queue(self, session_key: str) -> collections.deque[InboundEvent]:
        """Return a direct reference to the session's deque (for drain loops).

        The caller must not hold this reference across awaits in order to
        preserve the cooperative-asyncio safety contract.

        Args:
            session_key: Unique session identifier.

        Returns:
            The deque (empty deque if session not initialised).
        """
        return self._queues.get(session_key, collections.deque())

    def depth(self, session_key: str) -> int:
        """Return the current queue depth for a session.

        Args:
            session_key: Unique session identifier.

        Returns:
            Number of queued events (0 if session unknown).
        """
        q = self._queues.get(session_key)
        return len(q) if q is not None else 0

    def ensure_session(self, session_key: str) -> None:
        """Initialise queue structures for a session if not already present.

        Safe to call multiple times — idempotent.

        Args:
            session_key: Unique session identifier.
        """
        self._ensure_session(session_key)

    def snapshot(self, session_key: str) -> list[InboundEvent]:
        """Return a list snapshot of the session queue (for testing introspection).

        Args:
            session_key: Unique session identifier.

        Returns:
            List of queued events (empty list if session unknown).
        """
        q = self._queues.get(session_key)
        return list(q) if q is not None else []

    def cleanup_idle(self, *, now: float | None = None) -> int:
        """Remove sessions whose last activity exceeds the idle TTL.

        Should be called periodically (e.g. from a background task) to
        prevent unbounded memory growth from abandoned sessions.

        Args:
            now: Override current time (seconds). Defaults to time.time().

        Returns:
            Number of sessions evicted.
        """
        current = now if now is not None else time.time()
        cutoff = current - self._idle_ttl
        to_remove = [key for key, last in self._last_active.items() if last < cutoff]
        for key in to_remove:
            self._queues.pop(key, None)
            self._last_active.pop(key, None)
            _logger.debug("QueueManager: evicted idle session %s", key)

        if to_remove:
            _logger.info(
                "QueueManager.cleanup_idle: evicted %d idle sessions",
                len(to_remove),
            )
        return len(to_remove)

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _ensure_session(self, session_key: str) -> None:
        """Initialise queue and activity timestamp for a session."""
        if session_key not in self._queues:
            self._queues[session_key] = collections.deque()
            self._last_active[session_key] = time.time()

    def _touch(self, session_key: str) -> None:
        """Update the last-active timestamp for a session."""
        self._last_active[session_key] = time.time()
