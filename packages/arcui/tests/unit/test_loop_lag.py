"""The opt-in event-loop lag monitor sees a blocked loop and reports it."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

import pytest

from arcui.loop_lag import LagReport, LoopLagMonitor, summarize


def test_summarize_reports_percentiles_in_milliseconds() -> None:
    samples = [0.001] * 98 + [0.2, 0.5]
    report = summarize(samples)
    assert report.samples == 100
    assert report.p50_ms == pytest.approx(1.0)
    assert report.p99_ms == pytest.approx(200.0)
    assert report.max_ms == pytest.approx(500.0)


async def test_a_blocking_call_on_the_loop_shows_up_in_the_report() -> None:
    reports: list[LagReport] = []
    reported = asyncio.Event()

    def collect(report: LagReport) -> None:
        reports.append(report)
        reported.set()

    monitor = LoopLagMonitor(interval=0.01, report_every=0.2, report=collect)
    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0.03)
    time.sleep(0.25)  # the blocking call under test: it holds the loop thread
    await asyncio.wait_for(reported.wait(), timeout=5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert reports[0].max_ms >= 200, reports[0]


async def test_a_slow_window_is_logged_as_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="arcui.loop_lag")
    LoopLagMonitor.log_report(LagReport(samples=10, p50_ms=1.0, p99_ms=150.0, max_ms=900.0))
    LoopLagMonitor.log_report(LagReport(samples=10, p50_ms=1.0, p99_ms=5.0, max_ms=9.0))
    levels = [record.levelno for record in caplog.records]
    assert levels == [logging.WARNING, logging.INFO]
    assert "p99=150.0ms" in caplog.records[0].getMessage()
