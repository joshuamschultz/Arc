from __future__ import annotations

import asyncio

import pytest

from arcagent.utils.periodic import FailurePolicy, PeriodicRunner


@pytest.mark.asyncio
async def test_stop_interrupts_long_idle_wait_immediately() -> None:
    runner = PeriodicRunner()
    ticked = asyncio.Event()

    async def tick() -> None:
        ticked.set()

    task = asyncio.create_task(runner.run(tick, interval=3600))
    await asyncio.wait_for(ticked.wait(), timeout=1)
    runner.stop()
    await asyncio.wait_for(task, timeout=0.1)


@pytest.mark.asyncio
async def test_dynamic_interval_is_sampled_once_per_cadence() -> None:
    runner = PeriodicRunner()
    intervals = iter((0.01, 3600.0))
    calls = 0
    second = asyncio.Event()

    async def tick() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            second.set()

    task = asyncio.create_task(runner.run(tick, interval=lambda: next(intervals)))
    await asyncio.wait_for(second.wait(), timeout=1)
    runner.stop()
    await task
    assert calls == 2


@pytest.mark.asyncio
async def test_failure_threshold_and_error_counts_are_explicit() -> None:
    runner = PeriodicRunner()
    seen: list[int] = []

    async def tick() -> None:
        raise RuntimeError("boom")

    await runner.run(
        tick,
        interval=0.001,
        failure=FailurePolicy(max_consecutive=3, backoff_factor=2, max_backoff_seconds=0.01),
        on_error=lambda _exc, failures: seen.append(failures),
    )
    assert seen == [1, 2, 3]
