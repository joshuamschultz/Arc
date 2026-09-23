"""A connection may use only a fraction of wall time, one unit of work at a time.

Operator's words: a connection must never take all the CPU. A first backfill
may run for days; after it, only the changes. Every connector goes through the
same limiter — there is no per-vendor branch.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from arcagent.connected_data import SyncLimits
from arcagent.modules.connected_data.pacing import DutyCycleLimiter


class FakeTime:
    """A clock that only moves when work or sleep says so."""

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


async def test_rest_after_each_unit_holds_the_source_to_its_fraction() -> None:
    time = FakeTime()
    limiter = DutyCycleLimiter(0.25, clock=time.clock, sleep=time.sleep)
    start = time.now

    for _ in range(4):
        async with limiter.unit():
            time.now += 1.0  # one second of work

    busy = 4.0
    # The last unit's rest is owed to whatever comes next.
    assert time.sleeps == [3.0, 3.0, 3.0]
    assert busy / (time.now - start + 3.0) == pytest.approx(0.25)
    assert limiter.rested == pytest.approx(9.0)


async def test_concurrent_units_of_one_source_run_one_at_a_time() -> None:
    time = FakeTime()
    limiter = DutyCycleLimiter(0.5, clock=time.clock, sleep=time.sleep)
    running = 0
    peak = 0

    async def work() -> None:
        nonlocal running, peak
        async with limiter.unit():
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0)
            time.now += 2.0
            running -= 1

    await asyncio.gather(*(work() for _ in range(5)))

    assert peak == 1
    assert time.sleeps == [2.0, 2.0, 2.0, 2.0]


async def test_full_duty_never_sleeps_but_still_yields_the_loop() -> None:
    time = FakeTime()
    limiter = DutyCycleLimiter(1.0, clock=time.clock, sleep=time.sleep)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0)
            ticks += 1

    task = asyncio.create_task(ticker())
    for _ in range(10):
        async with limiter.unit():
            time.now += 1.0
    task.cancel()

    assert time.sleeps == []
    assert ticks >= 9, "every unit must hand the loop to others first"


async def test_one_rest_is_bounded() -> None:
    time = FakeTime()
    limiter = DutyCycleLimiter(0.1, clock=time.clock, sleep=time.sleep, max_rest=30.0)

    async with limiter.unit():
        time.now += 100.0
    async with limiter.unit():
        pass

    assert time.sleeps == [30.0]


@pytest.mark.parametrize("fraction", [0.0, -0.1, 1.01])
def test_the_fraction_must_be_a_share_of_wall_time(fraction: float) -> None:
    with pytest.raises(ValidationError):
        SyncLimits(max_duty_fraction=fraction)
    with pytest.raises(ValueError):
        DutyCycleLimiter(fraction)


def test_the_default_leaves_three_quarters_of_the_time_to_everything_else() -> None:
    assert SyncLimits().max_duty_fraction == 0.25
