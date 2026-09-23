"""When each connected source runs next: its interval, a continuation, or a backoff.

One schedule serves every connector; nothing here knows a vendor. A source
that finished its account waits a full interval and then fetches only the
changes. A run that stopped at its budget continues promptly, so a first
backfill keeps going (paced, see ``pacing``) until it is done. A run that
crashed or hung is retried after a capped exponential backoff — soon enough to
recover from a blip, never so fast that a broken source spins.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable


def _jitter() -> float:
    """A factor in [0.9, 1.1] so many sources failing together do not retry in step."""
    return 0.9 + secrets.randbelow(201) / 1000


class SyncSchedule:
    """Per-source due times and consecutive-failure counts."""

    def __init__(
        self,
        *,
        interval_seconds: float,
        backoff_seconds: float,
        backoff_max_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = _jitter,
    ) -> None:
        self._interval = interval_seconds
        self._backoff = backoff_seconds
        self._backoff_max = backoff_max_seconds
        self._clock = clock
        self._jitter = jitter
        self._due: dict[str, float] = {}
        self._failures: dict[str, int] = {}
        self._requested: set[str] = set()

    def is_due(self, connection_id: str) -> bool:
        """True for a source never run in this process, or whose time has come."""
        due = self._due.get(connection_id)
        return due is None or due <= self._clock()

    def run_now(self, connection_id: str) -> None:
        """An operator asked for this source (sync now, resume, reindex).

        Remembered until a run starts, so a request made while a run is still
        going is honoured when it ends instead of being dropped.
        """
        self._due[connection_id] = self._clock()
        self._requested.add(connection_id)

    def started(self, connection_id: str) -> None:
        """A run began; it answers every request made before this point."""
        self._requested.discard(connection_id)

    def completed(self, connection_id: str, *, more_work: bool) -> None:
        """A run ended cleanly; continue at once if it stopped at its budget."""
        self._failures.pop(connection_id, None)
        now = self._clock()
        prompt = more_work or connection_id in self._requested
        self._due[connection_id] = now if prompt else now + self._interval

    def failed(self, connection_id: str) -> float:
        """A run crashed or hung; return the backoff before it is tried again."""
        count = self._failures.get(connection_id, 0) + 1
        self._failures[connection_id] = count
        growth: float = 2.0 ** (count - 1)
        delay = min(self._backoff * growth, self._backoff_max) * self._jitter()
        self._due[connection_id] = self._clock() + delay
        return delay

    def failures(self, connection_id: str) -> int:
        """Consecutive failures since the source last completed."""
        return self._failures.get(connection_id, 0)

    def forget(self, connection_id: str) -> None:
        """Drop a revoked source's timing."""
        self._due.pop(connection_id, None)
        self._failures.pop(connection_id, None)
        self._requested.discard(connection_id)

    def seconds_until_next(self) -> float:
        """How long the monitor may sleep before some source is due."""
        now = self._clock()
        waits = [due - now for due in self._due.values()]
        return max(0.0, min([self._interval, *waits]))


__all__ = ["SyncSchedule"]
