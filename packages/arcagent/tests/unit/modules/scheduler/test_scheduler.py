"""Unit tests for scheduler engine — SPEC-002 Phase 3."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
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


class TestQueueDedup:
    @pytest.mark.asyncio
    async def test_duplicate_enqueue_skipped(self) -> None:
        engine = SchedulerEngine(
            store=MagicMock(),
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=AsyncMock(),
        )
        entry = make_entry(id="sched_dedup")
        await engine.enqueue(entry)
        await engine.enqueue(entry)  # duplicate
        assert engine._queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_different_ids_both_enqueued(self) -> None:
        engine = SchedulerEngine(
            store=MagicMock(),
            config=make_config(),
            telemetry=MagicMock(),
            agent_run_fn=AsyncMock(),
        )
        await engine.enqueue(make_entry(id="sched_a"))
        await engine.enqueue(make_entry(id="sched_b"))
        assert engine._queue.qsize() == 2


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
        entry = make_entry(prompt="Check inbox")
        result = await engine.execute(entry)
        agent_run_fn.assert_awaited_once_with(
            "Check inbox",
            tool_choice={"type": "any"},
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
        async def slow_run(prompt: str) -> str:
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
    async def test_stop_drains_queue(self) -> None:
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
        await engine.start()

        # Enqueue work directly
        entry = make_entry(prompt="drain test")
        await engine.enqueue(entry)

        # Give worker a moment to process
        await asyncio.sleep(0.1)
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
