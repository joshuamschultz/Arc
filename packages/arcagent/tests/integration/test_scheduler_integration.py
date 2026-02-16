"""Integration test for scheduler module — SPEC-002 Phase 6.

End-to-end verification with mock agent_run_fn. Tests:
- Schedule creation via tool
- Schedule fire evaluation
- Execution with metadata update
- Circuit breaker behavior
- Graceful shutdown
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from arcagent.modules.scheduler import SchedulerModule
from arcagent.modules.scheduler.models import ScheduleEntry
from arcagent.modules.scheduler.store import ScheduleStore
from arcagent.modules.scheduler.tools import create_scheduler_tools


def _make_config() -> MagicMock:
    cfg = MagicMock()
    cfg.max_schedules = 50
    cfg.max_prompt_length = 500
    cfg.min_interval_seconds = 60
    cfg.default_timeout_seconds = 300
    cfg.max_timeout_seconds = 3600
    cfg.circuit_breaker_threshold = 3
    cfg.check_interval_seconds = 30
    cfg.store_path = "schedules.json"
    cfg.enabled = True
    return cfg


class TestSchedulerIntegration:
    """End-to-end tests for the scheduler module."""

    @pytest.mark.asyncio
    async def test_create_and_fire_interval_schedule(self, tmp_path: Path) -> None:
        """Create an interval schedule via tool, then fire it via engine."""
        config = _make_config()
        store = ScheduleStore(tmp_path / "schedules.json")
        telemetry = MagicMock()
        run_results: list[str] = []

        async def mock_agent_run(prompt: str) -> str:
            run_results.append(prompt)
            return "done"

        # Create tools and use schedule_create
        tools = create_scheduler_tools(store=store, config=config, telemetry=telemetry)
        create_tool = next(t for t in tools if t.name == "schedule_create")

        result = await create_tool.execute(
            type="interval", prompt="Check inbox", every_seconds=300,
        )
        data = json.loads(result)
        assert data["type"] == "interval"
        schedule_id = data["id"]

        # Verify persisted
        entries = store.load()
        assert len(entries) == 1
        assert entries[0].id == schedule_id

        # Create module with the mock run function
        module = SchedulerModule(config=config, telemetry=telemetry, workspace=tmp_path)
        ctx = MagicMock()
        ctx.tool_registry = MagicMock()
        ctx.bus = MagicMock()
        ctx.agent_run_fn = mock_agent_run
        await module.startup(ctx)

        # Manually enqueue the entry (simulating a fire)
        entry = store.get(schedule_id)
        assert entry is not None
        assert module._engine is not None
        await module._engine.enqueue(entry)

        # Give worker time to process
        await asyncio.sleep(0.2)

        # Verify agent.run() was called
        assert "Check inbox" in run_results

        # Verify metadata was updated
        updated = store.get(schedule_id)
        assert updated is not None
        assert updated.metadata.run_count == 1
        assert updated.metadata.last_result == "ok"
        assert updated.metadata.last_run is not None

        await module.shutdown()

    @pytest.mark.asyncio
    async def test_list_and_cancel_schedules(self, tmp_path: Path) -> None:
        """Create multiple schedules, list them, cancel one."""
        config = _make_config()
        store = ScheduleStore(tmp_path / "schedules.json")
        tools = create_scheduler_tools(store=store, config=config, telemetry=MagicMock())

        create_tool = next(t for t in tools if t.name == "schedule_create")
        list_tool = next(t for t in tools if t.name == "schedule_list")
        cancel_tool = next(t for t in tools if t.name == "schedule_cancel")

        # Create 2 schedules
        r1 = json.loads(await create_tool.execute(
            type="interval", prompt="Heartbeat", every_seconds=300,
        ))
        r2 = json.loads(await create_tool.execute(
            type="cron", prompt="Daily report", expression="0 9 * * *",
        ))

        # List all
        all_entries = json.loads(await list_tool.execute())
        assert len(all_entries) == 2

        # Cancel (disable) first
        cancel_result = json.loads(await cancel_tool.execute(id=r1["id"]))
        assert cancel_result["status"] == "disabled"

        # List enabled only
        enabled = json.loads(await list_tool.execute(enabled_only=True))
        assert len(enabled) == 1
        assert enabled[0]["id"] == r2["id"]

    @pytest.mark.asyncio
    async def test_circuit_breaker_integration(self, tmp_path: Path) -> None:
        """Verify circuit breaker disables schedule after repeated failures."""
        config = _make_config()
        store = ScheduleStore(tmp_path / "schedules.json")
        telemetry = MagicMock()

        async def failing_run(prompt: str) -> str:
            raise RuntimeError("LLM unavailable")

        module = SchedulerModule(config=config, telemetry=telemetry, workspace=tmp_path)
        ctx = MagicMock()
        ctx.tool_registry = MagicMock()
        ctx.bus = MagicMock()
        ctx.agent_run_fn = failing_run
        await module.startup(ctx)

        # Add a schedule directly
        entry = ScheduleEntry(
            id="sched_fail_test", type="interval",
            prompt="Will fail", every_seconds=300,
        )
        store.add(entry)

        # Execute 3 times (circuit breaker threshold)
        assert module._engine is not None
        for _ in range(3):
            await module._engine.execute(entry)

        # Verify schedule is disabled
        updated = store.get("sched_fail_test")
        assert updated is not None
        assert updated.enabled is False

        await module.shutdown()

    @pytest.mark.asyncio
    async def test_graceful_shutdown_drains_queue(self, tmp_path: Path) -> None:
        """Queue items should be processed before shutdown completes."""
        config = _make_config()
        store = ScheduleStore(tmp_path / "schedules.json")
        results: list[str] = []

        async def mock_run(prompt: str) -> str:
            results.append(prompt)
            return "ok"

        module = SchedulerModule(config=config, telemetry=MagicMock(), workspace=tmp_path)
        ctx = MagicMock()
        ctx.tool_registry = MagicMock()
        ctx.bus = MagicMock()
        ctx.agent_run_fn = mock_run
        await module.startup(ctx)

        # Add entry and enqueue
        entry = ScheduleEntry(
            id="sched_drain", type="interval",
            prompt="Drain test", every_seconds=300,
        )
        store.add(entry)
        assert module._engine is not None
        await module._engine.enqueue(entry)

        # Small delay for worker to pick up
        await asyncio.sleep(0.1)

        # Shutdown should drain queue
        await module.shutdown()
        assert "Drain test" in results
