"""SPEC-017 Phase 6 Tasks 6.4-6.21 — ProactiveEngine.

Covers:
  * Min-heap priority queue driven by monotonic time
  * Drift-free rescheduling (``last_actual_run + interval``)
  * Clock warp detection (``time.time()`` vs ``time.monotonic()``)
  * Concurrency policy: in-flight tick skips new dispatch
  * Wake-event idempotency (timestamp-based discard)
  * Active-hours gating (R-049 — out-of-window ticks are suppressed)
  * Circuit-breaker integration per schedule

Tests drive a fake monotonic clock so timing is deterministic.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class TestScheduleModel:
    """Schedule payload held in the heap."""

    def test_schedule_has_next_run_and_interval(self) -> None:
        from arcagent.modules.proactive.engine import Schedule

        sched = Schedule(
            id="s1",
            interval_seconds=60.0,
            next_run_monotonic=100.0,
            kind="cron",
        )
        assert sched.id == "s1"
        assert sched.interval_seconds == 60.0
        assert sched.next_run_monotonic == 100.0
        assert sched.kind == "cron"
        assert sched.in_flight is False


class TestTickDispatch:
    """Task 6.4-6.5 — tick loop dispatches when ``next_run`` elapses."""

    async def test_tick_dispatches_due_schedule(self) -> None:
        from arcagent.modules.proactive.engine import ProactiveEngine, Schedule

        clock = _FakeClock()
        fired: list[str] = []

        async def handler(sched: Schedule) -> None:
            fired.append(sched.id)

        engine = ProactiveEngine(
            handler=handler,
            monotonic=clock,
            poll_interval_seconds=1.0,
        )
        engine.add(Schedule(id="a", interval_seconds=10, next_run_monotonic=5, kind="cron"))

        # Not yet due
        await engine.tick()
        assert fired == []

        # Past due
        clock.advance(6)
        await engine.tick()
        # Let the dispatched task run
        await asyncio.sleep(0)
        assert fired == ["a"]


class TestDriftFreeRescheduling:
    """Task 6.6-6.7 — reschedule = last_actual_run + interval (- overhead)."""

    async def test_next_run_based_on_last_actual_not_wall_time(self) -> None:
        from arcagent.modules.proactive.engine import ProactiveEngine, Schedule

        clock = _FakeClock()

        async def handler(sched: Schedule) -> None:
            # Simulate slow handler
            await asyncio.sleep(0)

        engine = ProactiveEngine(handler=handler, monotonic=clock)
        engine.add(Schedule(id="a", interval_seconds=10, next_run_monotonic=10, kind="cron"))

        clock.advance(15)  # 5s past due
        await engine.tick()
        await asyncio.sleep(0)

        sched = engine.get("a")
        # Reschedule computes from last_actual_run (=15), not now + interval
        # last_actual_run + interval - overhead_shim (0.010)
        assert sched is not None
        assert sched.next_run_monotonic == pytest.approx(25.0 - 0.010)


class TestConcurrencyPolicy:
    """Task 6.9-6.10 — CronJob ``concurrencyPolicy: Forbid`` semantics."""

    async def test_in_flight_skip_emits_miss(self) -> None:
        from arcagent.modules.proactive.engine import ProactiveEngine, Schedule

        clock = _FakeClock()
        events: list[tuple[str, dict[str, Any]]] = []

        # Block the handler so it stays in-flight
        gate = asyncio.Event()

        async def handler(sched: Schedule) -> None:
            await gate.wait()

        engine = ProactiveEngine(
            handler=handler,
            monotonic=clock,
            event_sink=lambda event, data: events.append((event, data)),
        )
        engine.add(Schedule(id="s", interval_seconds=1, next_run_monotonic=1, kind="cron"))

        # First tick starts the handler
        clock.advance(2)
        await engine.tick()
        await asyncio.sleep(0)  # let dispatch task start
        await asyncio.sleep(0)  # and reach ``gate.wait()``
        assert engine.get("s") is not None
        assert engine.get("s").in_flight is True

        # Second tick before first finishes — should emit miss
        clock.advance(2)
        await engine.tick()

        miss_events = [e for e in events if e[0] == "missed_concurrency"]
        assert len(miss_events) == 1

        # Release handler + drain in-flight tasks so the test doesn't
        # leave pending coroutines hanging off the event loop.
        gate.set()
        await engine.drain()


class TestWakeIdempotency:
    """Task 6.11-6.12 — wakes with timestamp <= last_wake are discarded."""

    async def test_stale_wake_discarded(self) -> None:
        from arcagent.modules.proactive.engine import ProactiveEngine

        engine = ProactiveEngine(handler=_noop_handler)
        assert engine.handle_wake(timestamp_us=100) is True
        assert engine.handle_wake(timestamp_us=100) is False  # duplicate
        assert engine.handle_wake(timestamp_us=99) is False  # stale
        assert engine.handle_wake(timestamp_us=101) is True


class TestClockWarpDetection:
    """Task 6.8 — warn when wall-clock and monotonic diverge.

    Protection against VM suspend / NTP jump. The engine doesn't
    refuse to run — it logs a structured warning so ops can correlate.
    """

    async def test_warp_warning_emitted_when_threshold_exceeded(self) -> None:
        from arcagent.modules.proactive.engine import ProactiveEngine

        events: list[tuple[str, dict[str, Any]]] = []

        engine = ProactiveEngine(
            handler=_noop_handler,
            event_sink=lambda ev, data: events.append((ev, data)),
            clock_warp_threshold_seconds=5.0,
        )

        # Simulate a clock warp — wall clock jumped 10s but monotonic only 1s
        engine.check_clock_warp(monotonic_delta=1.0, wall_delta=11.0)
        warp_events = [e for e in events if e[0] == "clock_warp"]
        assert len(warp_events) == 1
        assert warp_events[0][1]["delta_seconds"] == pytest.approx(10.0)

    async def test_no_warning_under_threshold(self) -> None:
        from arcagent.modules.proactive.engine import ProactiveEngine

        events: list[tuple[str, dict[str, Any]]] = []
        engine = ProactiveEngine(
            handler=_noop_handler,
            event_sink=lambda ev, data: events.append((ev, data)),
            clock_warp_threshold_seconds=5.0,
        )
        engine.check_clock_warp(monotonic_delta=10.0, wall_delta=10.5)
        assert [e for e in events if e[0] == "clock_warp"] == []


class TestCircuitBreakerIntegration:
    """Open breaker short-circuits dispatch; emits ``skipped_circuit_open``."""

    async def test_open_breaker_skips_dispatch(self) -> None:
        from arcagent.modules.proactive.circuit_breaker import CircuitBreaker
        from arcagent.modules.proactive.engine import ProactiveEngine, Schedule

        clock = _FakeClock()
        events: list[tuple[str, dict[str, Any]]] = []
        fired: list[str] = []

        async def handler(sched: Schedule) -> None:
            fired.append(sched.id)

        engine = ProactiveEngine(
            handler=handler,
            monotonic=clock,
            event_sink=lambda ev, data: events.append((ev, data)),
        )
        breaker = CircuitBreaker(failure_threshold=1, base_wait_seconds=100, monotonic=clock)
        breaker.record_failure()  # OPEN
        engine.add(
            Schedule(
                id="blocked",
                interval_seconds=10,
                next_run_monotonic=5,
                kind="cron",
                circuit_breaker=breaker,
            )
        )

        clock.advance(10)
        await engine.tick()
        await asyncio.sleep(0)

        assert fired == []
        assert any(e[0] == "skipped_circuit_open" for e in events)


class TestActiveHoursGating:
    """R-049 — a schedule carrying ActiveHours is suppressed outside its window.

    Drives the real ``_dispatch`` path (via ``tick``) with a fixed injected wall
    clock so the ActiveHours implementation from ``timezone.py`` is actually
    consulted on the engine's dispatch path, not just in isolation.
    """

    async def test_out_of_window_tick_is_suppressed_and_rescheduled(self) -> None:
        from datetime import UTC, datetime, time

        from arcagent.modules.proactive.engine import ProactiveEngine, Schedule
        from arcagent.modules.proactive.timezone import ActiveHours

        clock = _FakeClock()
        fired: list[str] = []
        events: list[tuple[str, dict[str, Any]]] = []

        async def handler(sched: Schedule) -> None:
            fired.append(sched.id)

        # 03:00 UTC is outside a 09:00–17:00 UTC window.
        wall = datetime(2026, 4, 18, 3, 0, tzinfo=UTC)
        engine = ProactiveEngine(
            handler=handler,
            monotonic=clock,
            wall_clock=lambda: wall,
            event_sink=lambda ev, data: events.append((ev, data)),
        )
        engine.add(
            Schedule(
                id="quiet",
                interval_seconds=10,
                next_run_monotonic=5,
                kind="cron",
                active_hours=ActiveHours(tz="UTC", start=time(9, 0), end=time(17, 0)),
            )
        )

        clock.advance(6)
        await engine.tick()
        await asyncio.sleep(0)

        assert fired == []
        assert any(e[0] == "skipped_inactive_hours" for e in events)
        # Suppressed tick is deferred, not dropped — rescheduled past ``now``
        # (now=6 + interval=10 - overhead shim).
        sched = engine.get("quiet")
        assert sched is not None
        assert sched.next_run_monotonic == pytest.approx(16.0 - 0.010)

    async def test_in_window_tick_dispatches(self) -> None:
        from datetime import UTC, datetime, time

        from arcagent.modules.proactive.engine import ProactiveEngine, Schedule
        from arcagent.modules.proactive.timezone import ActiveHours

        clock = _FakeClock()
        fired: list[str] = []

        async def handler(sched: Schedule) -> None:
            fired.append(sched.id)

        # 12:00 UTC is inside the 09:00–17:00 UTC window.
        wall = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)
        engine = ProactiveEngine(handler=handler, monotonic=clock, wall_clock=lambda: wall)
        engine.add(
            Schedule(
                id="active",
                interval_seconds=10,
                next_run_monotonic=5,
                kind="cron",
                active_hours=ActiveHours(tz="UTC", start=time(9, 0), end=time(17, 0)),
            )
        )

        clock.advance(6)
        await engine.tick()
        await asyncio.sleep(0)

        assert fired == ["active"]


# --- helpers --------------------------------------------------------------


async def _noop_handler(_sched: Any) -> None:
    return None
