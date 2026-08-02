"""Unit tests for scheduler engine — SPEC-002 Phase 3."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from freezegun import freeze_time

from arcagent.modules.scheduler.models import (
    ActiveHours,
    ScheduleEntry,
    ScheduleMetadata,
)
from arcagent.modules.scheduler.scheduler import SchedulerEngine
from arcagent.modules.scheduler.store import ScheduleStore
from tests.unit.modules.scheduler.conftest import make_config, make_entry

# --- should_fire evaluation ---


class TestShouldFireInterval:
    def test_fires_when_elapsed(self) -> None:
        engine = SchedulerEngine(
            store=MagicMock(),
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=AsyncMock(),
        )
        entry = make_entry(
            every_seconds=300,
            metadata=ScheduleMetadata(
                last_run=(datetime.now(tz=UTC) - timedelta(seconds=301)).isoformat(),
            ),
        )
        assert engine.should_fire(entry) is True

    def test_does_not_fire_when_not_elapsed(self) -> None:
        engine = SchedulerEngine(
            store=MagicMock(),
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=AsyncMock(),
        )
        entry = make_entry(
            every_seconds=300,
            metadata=ScheduleMetadata(
                last_run=(datetime.now(tz=UTC) - timedelta(seconds=100)).isoformat(),
            ),
        )
        assert engine.should_fire(entry) is False

    def test_fires_first_run_no_last_run(self) -> None:
        engine = SchedulerEngine(
            store=MagicMock(),
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=AsyncMock(),
        )
        entry = make_entry(every_seconds=300)
        assert engine.should_fire(entry) is True

    def test_disabled_does_not_fire(self) -> None:
        engine = SchedulerEngine(
            store=MagicMock(),
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=AsyncMock(),
        )
        entry = make_entry(every_seconds=300, enabled=False)
        assert engine.should_fire(entry) is False


class TestShouldFireCron:
    @freeze_time("2026-02-16 09:01:00", tz_offset=0)
    def test_cron_fires_when_due(self) -> None:
        engine = SchedulerEngine(
            store=MagicMock(),
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=AsyncMock(),
        )
        entry = ScheduleEntry(
            id="sched_cron",
            type="cron",
            prompt="Daily check",
            expression="0 9 * * *",  # 09:00 daily
            metadata=ScheduleMetadata(
                last_run="2026-02-15T09:00:00+00:00",
            ),
        )
        assert engine.should_fire(entry) is True

    @freeze_time("2026-02-16 08:30:00", tz_offset=0)
    def test_cron_does_not_fire_before_time(self) -> None:
        engine = SchedulerEngine(
            store=MagicMock(),
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=AsyncMock(),
        )
        entry = ScheduleEntry(
            id="sched_cron",
            type="cron",
            prompt="Daily check",
            expression="0 9 * * *",
            metadata=ScheduleMetadata(
                last_run="2026-02-15T09:00:00+00:00",
            ),
        )
        assert engine.should_fire(entry) is False


class TestShouldFireOnce:
    @freeze_time("2026-03-01 10:00:00", tz_offset=0)
    def test_once_fires_at_time(self) -> None:
        engine = SchedulerEngine(
            store=MagicMock(),
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=AsyncMock(),
        )
        entry = ScheduleEntry(
            id="sched_once",
            type="once",
            prompt="Send reminder",
            at="2026-03-01T09:00:00+00:00",
        )
        assert engine.should_fire(entry) is True

    @freeze_time("2026-02-28 10:00:00", tz_offset=0)
    def test_once_does_not_fire_before(self) -> None:
        engine = SchedulerEngine(
            store=MagicMock(),
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=AsyncMock(),
        )
        entry = ScheduleEntry(
            id="sched_once",
            type="once",
            prompt="Send reminder",
            at="2026-03-01T09:00:00+00:00",
        )
        assert engine.should_fire(entry) is False

    @freeze_time("2026-03-01 10:00:00", tz_offset=0)
    def test_once_does_not_fire_if_already_run(self) -> None:
        engine = SchedulerEngine(
            store=MagicMock(),
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=AsyncMock(),
        )
        entry = ScheduleEntry(
            id="sched_once",
            type="once",
            prompt="Send reminder",
            at="2026-03-01T09:00:00+00:00",
            metadata=ScheduleMetadata(run_count=1),
        )
        assert engine.should_fire(entry) is False


# --- Active hours ---


class TestActiveHoursCheck:
    @freeze_time("2026-02-16 14:00:00", tz_offset=0)
    def test_within_active_hours(self) -> None:
        engine = SchedulerEngine(
            store=MagicMock(),
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=AsyncMock(),
        )
        entry = make_entry(
            active_hours=ActiveHours(start="08:00", end="18:00", timezone="UTC"),
        )
        assert engine.is_within_active_hours(entry) is True

    @freeze_time("2026-02-16 05:00:00", tz_offset=0)
    def test_outside_active_hours(self) -> None:
        engine = SchedulerEngine(
            store=MagicMock(),
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=AsyncMock(),
        )
        entry = make_entry(
            active_hours=ActiveHours(start="08:00", end="18:00", timezone="UTC"),
        )
        assert engine.is_within_active_hours(entry) is False

    @freeze_time("2026-02-16 14:00:00", tz_offset=0)
    def test_no_active_hours_always_true(self) -> None:
        engine = SchedulerEngine(
            store=MagicMock(),
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=AsyncMock(),
        )
        entry = make_entry()
        assert engine.is_within_active_hours(entry) is True

    @freeze_time("2026-02-16 19:00:00", tz_offset=0)
    def test_timezone_conversion(self) -> None:
        """14:00 EST = 19:00 UTC. Active hours 08:00-18:00 EST -> should be active."""
        engine = SchedulerEngine(
            store=MagicMock(),
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=AsyncMock(),
        )
        entry = make_entry(
            active_hours=ActiveHours(
                start="08:00",
                end="18:00",
                timezone="US/Eastern",
            ),
        )
        assert engine.is_within_active_hours(entry) is True

    @freeze_time("2026-02-16 23:00:00", tz_offset=0)
    def test_overnight_within(self) -> None:
        """23:00 UTC is within 22:00-06:00 overnight window."""
        engine = SchedulerEngine(
            store=MagicMock(),
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=AsyncMock(),
        )
        entry = make_entry(
            active_hours=ActiveHours(start="22:00", end="06:00", timezone="UTC"),
        )
        assert engine.is_within_active_hours(entry) is True

    @freeze_time("2026-02-16 03:00:00", tz_offset=0)
    def test_overnight_within_early_morning(self) -> None:
        """03:00 UTC is within 22:00-06:00 overnight window."""
        engine = SchedulerEngine(
            store=MagicMock(),
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=AsyncMock(),
        )
        entry = make_entry(
            active_hours=ActiveHours(start="22:00", end="06:00", timezone="UTC"),
        )
        assert engine.is_within_active_hours(entry) is True

    @freeze_time("2026-02-16 12:00:00", tz_offset=0)
    def test_overnight_outside(self) -> None:
        """12:00 UTC is outside 22:00-06:00 overnight window."""
        engine = SchedulerEngine(
            store=MagicMock(),
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=AsyncMock(),
        )
        entry = make_entry(
            active_hours=ActiveHours(start="22:00", end="06:00", timezone="UTC"),
        )
        assert engine.is_within_active_hours(entry) is False


# --- Circuit breaker ---


class TestCircuitBreaker:
    def test_disables_after_threshold(self) -> None:
        store = MagicMock(spec=ScheduleStore)
        engine = SchedulerEngine(
            store=store,
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=AsyncMock(),
        )
        entry = make_entry(id="sched_fail")

        # Simulate 3 consecutive failures
        for _ in range(3):
            entry = engine.on_execution_failed(entry, RuntimeError("fail"))

        assert entry.enabled is False
        # Store.update should have been called to persist the disable
        store.update.assert_called()

    def test_does_not_disable_below_threshold(self) -> None:
        store = MagicMock(spec=ScheduleStore)
        engine = SchedulerEngine(
            store=store,
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=AsyncMock(),
        )
        entry = make_entry(id="sched_fail")

        # Only 2 failures
        for _ in range(2):
            entry = engine.on_execution_failed(entry, RuntimeError("fail"))

        assert entry.enabled is True

    def test_circuit_breaker_persists_in_metadata(self) -> None:
        """Consecutive failure count should be persisted in entry metadata."""
        store = MagicMock(spec=ScheduleStore)
        engine = SchedulerEngine(
            store=store,
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=AsyncMock(),
        )
        entry = make_entry(id="sched_fail")

        entry = engine.on_execution_failed(entry, RuntimeError("fail"))
        assert entry.metadata.consecutive_failures == 1

        entry = engine.on_execution_failed(entry, RuntimeError("fail"))
        assert entry.metadata.consecutive_failures == 2


# --- Queue dedup ---


class TestOverlapSkips:
    """A firing whose prior run is still going must skip, not stack.

    Tested through the tick, which is the only thing that fires now — the queue
    and worker this used to assert against are gone, and with them the loop that
    could park forever waiting on a readiness event.
    """

    @pytest.mark.asyncio
    async def test_a_second_tick_does_not_start_a_running_entry_again(self) -> None:
        entry = make_entry(id="sched_dedup")
        store = MagicMock()
        store.load.return_value = [entry]
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def run_fn(prompt: str, **kwargs: Any) -> str:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return "ok"

        engine = SchedulerEngine(
            store=store,
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=run_fn,
        )
        engine.should_fire = lambda e: True  # type: ignore[method-assign]

        first = asyncio.create_task(engine._tick())
        await asyncio.wait_for(started.wait(), timeout=1)
        await engine._tick()  # while the first is still running

        release.set()
        await first
        assert calls == 1

    @pytest.mark.asyncio
    async def test_two_different_entries_both_run(self) -> None:
        store = MagicMock()
        store.load.return_value = [make_entry(id="sched_a"), make_entry(id="sched_b")]
        ran: list[str] = []

        async def run_fn(prompt: str, **kwargs: Any) -> str:
            ran.append(kwargs.get("session_key", ""))
            return "ok"

        engine = SchedulerEngine(
            store=store,
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=run_fn,
        )
        engine.should_fire = lambda e: True  # type: ignore[method-assign]

        await engine._tick()

        assert ran == ["scheduler:sched_a", "scheduler:sched_b"]


class TestNothingFiresWithoutACallback:
    """A due entry with no agent callback stays pending — never consumed."""

    @pytest.mark.asyncio
    async def test_the_row_is_left_untouched(self) -> None:
        entry = make_entry(id="sched_pending")
        store = MagicMock()
        store.load.return_value = [entry]
        engine = SchedulerEngine(
            store=store,
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=None,
        )
        engine.should_fire = lambda e: True  # type: ignore[method-assign]

        await engine._tick()

        store.update.assert_not_called()


# --- Once auto-disable ---


class TestOnceAutoDisable:
    @pytest.mark.asyncio
    async def test_once_disabled_after_execution(self) -> None:
        """Once-schedules should be auto-disabled after successful execution."""
        agent_run_fn = AsyncMock(return_value="done")
        store = MagicMock(spec=ScheduleStore)
        engine = SchedulerEngine(
            store=store,
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=agent_run_fn,
        )
        entry = ScheduleEntry(
            id="sched_once",
            type="once",
            prompt="Send reminder",
            at="2026-03-01T09:00:00+00:00",
        )
        await engine.execute(entry)
        # Verify store.update was called with enabled=False
        call_args = store.update.call_args
        updates = call_args[0][1]
        assert updates.get("enabled") is False


# --- Execution ---


class TestExecution:
    @pytest.mark.asyncio
    async def test_execute_calls_agent_run(self) -> None:
        agent_run_fn = AsyncMock(return_value="done")
        store = MagicMock(spec=ScheduleStore)
        engine = SchedulerEngine(
            store=store,
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=agent_run_fn,
        )
        entry = make_entry(prompt="Check inbox", id="sched_inbox")
        result = await engine.execute(entry)
        agent_run_fn.assert_awaited_once_with(
            "Check inbox",
            session_key="scheduler:sched_inbox",
        )
        assert result is not None

    @pytest.mark.asyncio
    async def test_execute_updates_metadata_on_success(self) -> None:
        agent_run_fn = AsyncMock(return_value="ok")
        store = MagicMock(spec=ScheduleStore)
        engine = SchedulerEngine(
            store=store,
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=agent_run_fn,
        )
        entry = make_entry()
        await engine.execute(entry)
        store.update.assert_called_once()
        call_args = store.update.call_args
        updates = call_args[0][1]
        assert "metadata" in updates

    @pytest.mark.asyncio
    async def test_execute_handles_timeout(self) -> None:
        async def slow_run(prompt: str, *, session_key: str) -> str:
            await asyncio.sleep(10)
            return "done"

        store = MagicMock(spec=ScheduleStore)
        config = make_config()
        config.default_timeout_seconds = 300
        engine = SchedulerEngine(
            store=store,
            config=config,
            telemetry=MagicMock(),
            agent_run_fn=slow_run,
        )
        entry = make_entry(timeout_seconds=1)
        # Should handle timeout gracefully, not raise
        await engine.execute(entry)
        # After timeout, metadata should show error
        store.update.assert_called_once()

    @pytest.mark.asyncio
    async def test_interval_not_auto_disabled(self) -> None:
        """Interval schedules should NOT be auto-disabled after execution."""
        agent_run_fn = AsyncMock(return_value="done")
        store = MagicMock(spec=ScheduleStore)
        engine = SchedulerEngine(
            store=store,
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=agent_run_fn,
        )
        entry = make_entry(type="interval", every_seconds=300)
        await engine.execute(entry)
        call_args = store.update.call_args
        updates = call_args[0][1]
        assert "enabled" not in updates


class TestCronTimezone:
    """Cron fires at the wall-clock time of the configured timezone, not UTC."""

    def _engine(self, tz: str) -> SchedulerEngine:
        cfg = make_config()
        cfg.timezone = tz
        return SchedulerEngine(
            store=MagicMock(),
            config=cfg,
            telemetry=MagicMock(),
            agent_run_fn=AsyncMock(),
        )

    @freeze_time("2026-07-15 13:30:00")  # 08:30 America/Chicago (CDT = UTC-5)
    def test_cron_fires_after_local_time_reached(self) -> None:
        engine = self._engine("America/Chicago")
        entry = make_entry(
            type="cron",
            expression="0 8 * * *",  # 8am local
            every_seconds=None,
            metadata=ScheduleMetadata(last_run="2026-07-14T13:05:00+00:00"),
        )
        assert engine.should_fire(entry) is True

    @freeze_time("2026-07-15 12:30:00")  # 07:30 America/Chicago — before 8am local
    def test_cron_does_not_fire_before_local_time(self) -> None:
        engine = self._engine("America/Chicago")
        entry = make_entry(
            type="cron",
            expression="0 8 * * *",
            every_seconds=None,
            metadata=ScheduleMetadata(last_run="2026-07-15T05:05:00+00:00"),
        )
        assert engine.should_fire(entry) is False

    @freeze_time("2026-07-15 08:30:00")  # 08:30 UTC
    def test_empty_timezone_evaluates_in_utc(self) -> None:
        engine = self._engine("")
        entry = make_entry(
            type="cron",
            expression="0 8 * * *",
            every_seconds=None,
            metadata=ScheduleMetadata(last_run="2026-07-14T08:05:00+00:00"),
        )
        assert engine.should_fire(entry) is True


class TestChannelDelivery:
    @pytest.mark.asyncio
    async def test_delivers_result_to_channel_when_deliver_to_set(self) -> None:
        agent_run_fn = AsyncMock(return_value=MagicMock(content="the answer"))
        deliver_fn = AsyncMock()
        store = MagicMock(spec=ScheduleStore)
        engine = SchedulerEngine(
            store=store,
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=agent_run_fn,
        )
        engine.set_channel_deliver_fn(deliver_fn)
        entry = make_entry(id="s1", deliver_to="telegram:999")
        await engine.execute(entry)
        deliver_fn.assert_awaited_once_with("telegram:999", "the answer")

    @pytest.mark.asyncio
    async def test_no_delivery_when_deliver_to_unset(self) -> None:
        agent_run_fn = AsyncMock(return_value=MagicMock(content="the answer"))
        deliver_fn = AsyncMock()
        engine = SchedulerEngine(
            store=MagicMock(spec=ScheduleStore),
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=agent_run_fn,
        )
        engine.set_channel_deliver_fn(deliver_fn)
        entry = make_entry(id="s1")  # no deliver_to
        await engine.execute(entry)
        deliver_fn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_crash_when_deliver_to_set_but_no_fn_bound(self) -> None:
        agent_run_fn = AsyncMock(return_value=MagicMock(content="x"))
        engine = SchedulerEngine(
            store=MagicMock(spec=ScheduleStore),
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=agent_run_fn,
        )
        entry = make_entry(id="s1", deliver_to="telegram:999")
        # No channel_deliver_fn bound — must not raise.
        await engine.execute(entry)

    @pytest.mark.asyncio
    async def test_delivery_failure_does_not_break_execution(self) -> None:
        agent_run_fn = AsyncMock(return_value=MagicMock(content="x"))
        deliver_fn = AsyncMock(side_effect=RuntimeError("send failed"))
        store = MagicMock(spec=ScheduleStore)
        engine = SchedulerEngine(
            store=store,
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=agent_run_fn,
        )
        engine.set_channel_deliver_fn(deliver_fn)
        entry = make_entry(id="s1", deliver_to="telegram:999")
        # Delivery raises, but execute() must still complete and record success.
        result = await engine.execute(entry)
        assert result is not None
        store.update.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_delivery_on_failed_run(self) -> None:
        agent_run_fn = AsyncMock(side_effect=RuntimeError("run failed"))
        deliver_fn = AsyncMock()
        engine = SchedulerEngine(
            store=MagicMock(spec=ScheduleStore),
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=agent_run_fn,
        )
        engine.set_channel_deliver_fn(deliver_fn)
        entry = make_entry(id="s1", deliver_to="telegram:999")
        await engine.execute(entry)
        deliver_fn.assert_not_awaited()


# --- Start/Stop lifecycle ---


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_and_stop(self) -> None:
        store = MagicMock(spec=ScheduleStore)
        store.load.return_value = []
        engine = SchedulerEngine(
            store=store,
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=AsyncMock(),
        )
        await engine.start()
        assert engine.running is True
        await engine.stop()
        assert engine.running is False

    @pytest.mark.asyncio
    async def test_a_due_entry_runs_and_stop_is_clean(self) -> None:
        """Queue should drain before stop completes."""
        results: list[str] = []

        async def mock_run(prompt: str, **kwargs: object) -> str:
            results.append(prompt)
            return "ok"

        store = MagicMock(spec=ScheduleStore)
        store.load.return_value = []
        store.update.return_value = make_entry()
        engine = SchedulerEngine(
            store=store,
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=mock_run,
        )
        # A due entry runs inline on the tick — there is no queue to drain,
        # which is the point: nothing can be left sitting in one.
        store.load.return_value = [make_entry(prompt="drain test")]
        engine.should_fire = lambda e: True  # type: ignore[method-assign]
        await engine.start()
        await asyncio.sleep(0.1)
        await engine._tick()
        await engine.stop()
        assert "drain test" in results

    @pytest.mark.asyncio
    async def test_set_agent_run_fn(self) -> None:
        """Public setter should update the callback."""
        original_fn = AsyncMock(return_value="original")
        new_fn = AsyncMock(return_value="new")
        engine = SchedulerEngine(
            store=MagicMock(),
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=original_fn,
        )
        engine.set_agent_run_fn(new_fn)
        assert engine._agent_run_fn is new_fn


# --- Live reload of on-disk edits (arcui schedule editor contract) ---


class TestLiveReload:
    """The timer loop calls ``store.load()`` every tick, so an external editor
    (the arcui operator PATCH) that atomically rewrites ``schedules.json`` is
    honored on the next tick without restarting the agent. This locks that
    contract, which the arcui schedule-edit route depends on."""

    @pytest.mark.asyncio
    async def test_timer_loop_picks_up_disk_edit(self, tmp_path: Path) -> None:
        store = ScheduleStore(tmp_path / "schedules.json")
        # Starts disabled — the loop must not fire it.
        store.save([make_entry(id="sched_reload", enabled=False, every_seconds=60)])

        cfg = make_config()
        cfg.check_interval_seconds = 0.01

        fired = asyncio.Event()
        run_fn = AsyncMock(side_effect=lambda *_a, **_k: fired.set())
        engine = SchedulerEngine(
            store=store, config=cfg, telemetry=MagicMock(), agent_run_fn=run_fn
        )
        engine.set_agent_run_fn(run_fn)
        await engine.start()
        try:
            await asyncio.sleep(0.05)
            assert not fired.is_set()  # disabled on disk -> never fires
            # An operator edit lands on disk; the loop reloads and fires it.
            store.save([make_entry(id="sched_reload", enabled=True, every_seconds=60)])
            await asyncio.wait_for(fired.wait(), timeout=2.0)
        finally:
            await engine.stop()
        run_fn.assert_awaited()


class TestPoisonRowDoesNotStopTheEngine:
    """Real-path guard for the 2026-07-27 incident.

    A hand-written row with an invalid field made ``store.load()`` raise on
    every tick. The timer loop's fail-open handler counted the errors and at 5
    consecutive ticks tripped the breaker: ``_running = False``, every schedule
    for that agent dead until the process restarted, with nothing shown in
    arcui. Drives the REAL engine + REAL store against a real poisoned file.
    """

    @pytest.mark.asyncio
    async def test_good_schedule_still_fires_beside_a_poison_row(self, tmp_path: Path) -> None:
        import json

        path = tmp_path / "schedules.json"
        good = make_entry(id="sched_good", enabled=True, every_seconds=60).model_dump()
        poison = make_entry(id="sched_bad", enabled=True, every_seconds=60).model_dump()
        poison["metadata"]["created_by"] = "operator"  # closed Literal -> unloadable
        path.write_text(json.dumps([poison, good]), encoding="utf-8")

        cfg = make_config()
        cfg.check_interval_seconds = 0.01

        fired = asyncio.Event()
        run_fn = AsyncMock(side_effect=lambda *_a, **_k: fired.set())
        engine = SchedulerEngine(
            store=ScheduleStore(path), config=cfg, telemetry=MagicMock(), agent_run_fn=run_fn
        )
        engine.set_agent_run_fn(run_fn)
        await engine.start()
        try:
            # Poison row is listed FIRST — an eager loader dies before reaching
            # the good one. Well past the 5-tick breaker window.
            await asyncio.wait_for(fired.wait(), timeout=2.0)
            await asyncio.sleep(0.1)
            assert engine.running, "breaker tripped — one bad row killed the engine"
        finally:
            await engine.stop()
