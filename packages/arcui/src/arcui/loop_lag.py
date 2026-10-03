"""Opt-in event-loop lag monitor for the ``arc ui start`` process.

One process hosts the dashboard, gateway, fleet agents and connected-data
syncs on a single asyncio loop. When any of them runs blocking work on the
loop thread, NATS replies, WebSocket handshakes and health checks all wait.
This monitor measures how late a short sleep wakes up (the loop's lag) and
logs p50/p99/max once per window, so a stall is visible in the journal as a
number instead of as downstream timeouts. Enabled with ``[ui]
loop_lag_monitor = true`` in gateway.toml; off by default.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass

_logger = logging.getLogger("arcui.loop_lag")

# A window whose p99 is at or above this is logged as a warning.
SLOW_P99_MS = 100.0


@dataclass(frozen=True)
class LagReport:
    """Loop lag over one reporting window, in milliseconds."""

    samples: int
    p50_ms: float
    p99_ms: float
    max_ms: float


def summarize(samples: list[float]) -> LagReport:
    """Percentiles of lag samples given in seconds (nearest-rank)."""
    ordered = sorted(samples)
    count = len(ordered)

    def rank(fraction: float) -> float:
        return ordered[max(0, math.ceil(fraction * count) - 1)] * 1000

    return LagReport(samples=count, p50_ms=rank(0.5), p99_ms=rank(0.99), max_ms=ordered[-1] * 1000)


class LoopLagMonitor:
    """Sample loop lag every ``interval`` seconds; report every ``report_every``."""

    def __init__(
        self,
        *,
        interval: float = 0.1,
        report_every: float = 60.0,
        report: Callable[[LagReport], None] | None = None,
    ) -> None:
        self._interval = interval
        self._report_every = report_every
        self._report = report or self.log_report

    async def run(self) -> None:
        """Run until cancelled."""
        samples: list[float] = []
        window_start = time.monotonic()
        while True:
            before = time.monotonic()
            await asyncio.sleep(self._interval)
            now = time.monotonic()
            samples.append(max(0.0, now - before - self._interval))
            if now - window_start >= self._report_every:
                self._report(summarize(samples))
                samples = []
                window_start = now

    @staticmethod
    def log_report(report: LagReport) -> None:
        """Log one window; a slow p99 is a warning."""
        level = logging.WARNING if report.p99_ms >= SLOW_P99_MS else logging.INFO
        _logger.log(
            level,
            "event loop lag p50=%.1fms p99=%.1fms max=%.1fms samples=%d",
            report.p50_ms,
            report.p99_ms,
            report.max_ms,
            report.samples,
        )


__all__ = ["SLOW_P99_MS", "LagReport", "LoopLagMonitor", "summarize"]
