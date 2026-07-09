"""Integration test for the scheduler module — SPEC-002 / SPEC-021.

End-to-end verification through the LIVE path: the decorator-form
``Scheduler`` capability + module-level ``schedule_*`` tools, wired via
``_runtime.configure`` exactly as the capability loader does in production.
Tests:
- Schedule creation via the schedule_create tool
- Execution with metadata update through the engine worker
- Circuit breaker behavior
- Graceful shutdown draining the queue
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from arcagent.modules.scheduler import _runtime
from arcagent.modules.scheduler.capabilities import (
    Scheduler,
    schedule_cancel,
    schedule_create,
    schedule_list,
)
from arcagent.modules.scheduler.models import ScheduleEntry


@pytest.fixture(autouse=True)
def _reset_runtime() -> Iterator[None]:
    _runtime.reset()
    yield
    _runtime.reset()


def _configure(tmp_path: Path, agent_run_fn: _runtime.AgentRunFn) -> None:
    _runtime.configure(
        config={"enabled": True},
        telemetry=MagicMock(),
        workspace=tmp_path,
        agent_run_fn=agent_run_fn,
    )


class TestSchedulerIntegration:
    """End-to-end tests for the scheduler capability."""

    @pytest.mark.asyncio
    async def test_create_and_fire_interval_schedule(self, tmp_path: Path) -> None:
        """Create an interval schedule via tool, then fire it via the engine."""
        run_results: list[str] = []

        async def mock_agent_run(prompt: str, **kwargs: object) -> str:
            run_results.append(prompt)
            return "done"

        _configure(tmp_path, mock_agent_run)
        st = _runtime.state()

        result = await schedule_create(
            type="interval",
            prompt="Check inbox",
            every_seconds=300,
        )
        data = json.loads(result)
        assert data["type"] == "interval"
        schedule_id = data["id"]

        entries = st.store.load()
        assert len(entries) == 1
        assert entries[0].id == schedule_id

        cap = Scheduler()
        await cap.setup(None)
        try:
            entry = st.store.get(schedule_id)
            assert entry is not None
            assert st.engine is not None
            await st.engine.enqueue(entry)
            await asyncio.sleep(0.2)

            assert "Check inbox" in run_results
            updated = st.store.get(schedule_id)
            assert updated is not None
            assert updated.metadata.run_count == 1
            assert updated.metadata.last_result == "ok"
            assert updated.metadata.last_run is not None
        finally:
            await cap.teardown()

    @pytest.mark.asyncio
    async def test_list_and_cancel_schedules(self, tmp_path: Path) -> None:
        """Create multiple schedules, list them, cancel one."""

        async def mock_run(prompt: str, **kwargs: object) -> str:
            return "ok"

        _configure(tmp_path, mock_run)

        r1 = json.loads(
            await schedule_create(type="interval", prompt="Heartbeat", every_seconds=300)
        )
        r2 = json.loads(
            await schedule_create(type="cron", prompt="Daily report", expression="0 9 * * *")
        )

        all_entries = json.loads(await schedule_list())
        assert len(all_entries) == 2

        cancel_result = json.loads(await schedule_cancel(id=r1["id"]))
        assert cancel_result["status"] == "disabled"

        enabled = json.loads(await schedule_list(enabled_only=True))
        assert len(enabled) == 1
        assert enabled[0]["id"] == r2["id"]

    @pytest.mark.asyncio
    async def test_circuit_breaker_integration(self, tmp_path: Path) -> None:
        """Verify circuit breaker disables schedule after repeated failures."""

        async def failing_run(prompt: str, **kwargs: object) -> str:
            raise RuntimeError("LLM unavailable")

        _configure(tmp_path, failing_run)
        st = _runtime.state()

        cap = Scheduler()
        await cap.setup(None)
        try:
            entry = ScheduleEntry(
                id="sched_fail_test",
                type="interval",
                prompt="Will fail",
                every_seconds=300,
            )
            st.store.add(entry)

            assert st.engine is not None
            for _ in range(3):
                await st.engine.execute(entry)

            updated = st.store.get("sched_fail_test")
            assert updated is not None
            assert updated.enabled is False
        finally:
            await cap.teardown()

    @pytest.mark.asyncio
    async def test_graceful_shutdown_drains_queue(self, tmp_path: Path) -> None:
        """Queue items should be processed before teardown completes."""
        results: list[str] = []

        async def mock_run(prompt: str, **kwargs: object) -> str:
            results.append(prompt)
            return "ok"

        _configure(tmp_path, mock_run)
        st = _runtime.state()

        cap = Scheduler()
        await cap.setup(None)

        entry = ScheduleEntry(
            id="sched_drain",
            type="interval",
            prompt="Drain test",
            every_seconds=300,
        )
        st.store.add(entry)
        assert st.engine is not None
        await st.engine.enqueue(entry)
        await asyncio.sleep(0.1)

        await cap.teardown()
        assert "Drain test" in results
