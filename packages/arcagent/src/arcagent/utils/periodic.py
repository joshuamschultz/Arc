"""One cancellation-aware runner for fixed-delay periodic work."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

Tick = Callable[[], Awaitable[None]]
Interval = float | Callable[[], float]
ErrorHandler = Callable[[BaseException, int], None]


@dataclass(frozen=True)
class FailurePolicy:
    """Explicit periodic failure behavior.

    ``max_consecutive`` stops the runner after that many adjacent failures.
    ``backoff_factor`` multiplies the next delay after failures and is capped by
    ``max_backoff_seconds`` when supplied. A successful tick resets the count.
    """

    max_consecutive: int | None = None
    backoff_factor: float = 1.0
    max_backoff_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.max_consecutive is not None and self.max_consecutive < 1:
            raise ValueError("max_consecutive must be positive")
        if not math.isfinite(self.backoff_factor) or self.backoff_factor < 1:
            raise ValueError("backoff_factor must be finite and >= 1")
        if self.backoff_factor > 1 and self.max_backoff_seconds is None:
            raise ValueError("backoff_factor > 1 requires max_backoff_seconds")
        if self.max_backoff_seconds is not None and (
            not math.isfinite(self.max_backoff_seconds) or self.max_backoff_seconds <= 0
        ):
            raise ValueError("max_backoff_seconds must be finite and positive")


class PeriodicRunner:
    """Own fixed-delay cadence and make idle waits immediately stoppable."""

    def __init__(self) -> None:
        self._stop = asyncio.Event()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def reset(self) -> None:
        self._stop.clear()

    def stop(self) -> None:
        self._stop.set()

    async def run(
        self,
        tick: Tick,
        *,
        interval: Interval,
        immediate: bool = True,
        failure: FailurePolicy | None = None,
        on_error: ErrorHandler | None = None,
    ) -> None:
        """Run until stopped, cancelled, or the failure threshold is reached."""
        failure = failure or FailurePolicy()
        failures = 0
        if not immediate and await self._wait(self._delay(interval, failures, failure)):
            return
        while not self._stop.is_set():
            try:
                await tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # reason: failure policy owns continuation
                failures += 1
                if on_error is not None:
                    on_error(exc, failures)
                if failure.max_consecutive is not None and failures >= failure.max_consecutive:
                    return
            else:
                failures = 0
            if await self._wait(self._delay(interval, failures, failure)):
                return

    async def _wait(self, delay: float) -> bool:
        if not math.isfinite(delay) or delay <= 0:
            raise ValueError("periodic interval must be finite and positive")
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except TimeoutError:
            return False
        return True

    @staticmethod
    def _delay(interval: Interval, failures: int, failure: FailurePolicy) -> float:
        base = interval() if callable(interval) else interval
        delay = base * failure.backoff_factor**failures
        if failure.max_backoff_seconds is not None:
            delay = min(delay, failure.max_backoff_seconds)
        return delay


__all__ = ["FailurePolicy", "PeriodicRunner"]
