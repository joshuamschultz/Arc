"""Hold one connected source to a share of wall time, one unit of work at a time.

The same limiter paces every connector; nothing here knows a vendor. A unit is
one page fetch, one object fetch or one object ingest. Units of a source run
one at a time, and after each the source rests ``busy * (1 - f) / f`` so that
over any stretch it works at most a fraction ``f`` of the time. Every unit
starts by handing the event loop to everything else (at least a zero sleep),
so a source can delay chat, NATS and health by one unit, never by a page.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager


class DutyCycleLimiter:
    """Serialize one source's work and rest after each unit."""

    def __init__(
        self,
        fraction: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_rest: float | None = None,
    ) -> None:
        if not 0 < fraction <= 1:
            raise ValueError("duty fraction must be in (0, 1]")
        self._fraction = fraction
        self._clock = clock
        self._sleep = sleep
        self._max_rest = max_rest
        self._lock = asyncio.Lock()
        self._resume_at = float("-inf")
        self.rested = 0.0

    @asynccontextmanager
    async def unit(self) -> AsyncIterator[None]:
        """Wait for this source's turn and rest, run one unit, then owe its rest."""
        async with self._lock:
            await self._rest()
            started = self._clock()
            try:
                yield
            finally:
                finished = self._clock()
                owed = max(0.0, finished - started) * (1.0 - self._fraction) / self._fraction
                if self._max_rest is not None:
                    owed = min(owed, self._max_rest)
                self._resume_at = finished + owed

    async def _rest(self) -> None:
        delay = self._resume_at - self._clock()
        if delay <= 0:
            # Even a source allowed the whole machine yields between units.
            await asyncio.sleep(0)
            return
        self.rested += delay
        await self._sleep(delay)


__all__ = ["DutyCycleLimiter"]
