"""SPEC-021 Task 3.2 — scheduler module decorator-form tests.

The new ``capabilities.py`` exposes:

  * one ``@capability`` class :class:`Scheduler` (lifecycle)
  * one ``@hook("agent:ready")`` function (binds run_fn)
  * four ``@tool`` functions (schedule CRUD)

Verifies:

  1. Loader registers the capability class as a :class:`LifecycleEntry`,
     the hook against ``agent:ready``, and all four tools as
     :class:`ToolEntry` instances.
  2. ``Scheduler.setup()`` opens the engine; ``Scheduler.teardown()``
     stops it (cancelling timer + draining in-flight worker).
  3. The ``agent:ready`` hook binds the run_fn into the running engine.
  4. The CRUD tools route through ``_runtime.state()``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.core.capability_loader import CapabilityLoader
from arcagent.core.capability_registry import (
    CapabilityRegistry,
    LifecycleEntry,
    ToolEntry,
)
from arcagent.modules.scheduler import _runtime


@pytest.fixture(autouse=True)
def _reset_runtime() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


@pytest.fixture
def configured(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    telemetry = MagicMock()
    _runtime.configure(
        config={"enabled": True},
        telemetry=telemetry,
        workspace=workspace,
    )
    return workspace


@pytest.mark.asyncio
class TestLoaderRegistration:
    async def test_capability_class_and_hook_register(self) -> None:
        from arcagent.modules.scheduler import capabilities as scheduler_caps

        module_dir = Path(scheduler_caps.__file__).parent
        reg = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=[("scheduler", module_dir)], registry=reg)
        await loader.scan_and_register()

        # Capability class registered as LifecycleEntry
        cap_entry = await reg.get_capability("scheduler")
        assert cap_entry is not None
        assert isinstance(cap_entry, LifecycleEntry)
        assert cap_entry.meta.name == "scheduler"

        # agent:ready hook registered
        ready_hooks = await reg.get_hooks("agent:ready")
        assert any(h.meta.name == "bind_agent_run_fn" for h in ready_hooks)

        # All four tools registered
        for tool_name in (
            "schedule_create",
            "schedule_list",
            "schedule_update",
            "schedule_cancel",
        ):
            entry = await reg.get_tool(tool_name)
            assert entry is not None, f"missing tool {tool_name}"
            assert isinstance(entry, ToolEntry)


@pytest.mark.asyncio
class TestCapabilityLifecycle:
    async def test_setup_starts_engine(self, configured: Path) -> None:
        from arcagent.modules.scheduler.capabilities import Scheduler

        # Inject a real run_fn so the engine becomes ready immediately.
        st = _runtime.state()
        st.agent_run_fn = AsyncMock(return_value="ok")

        cap = Scheduler()
        try:
            await cap.setup(None)
            assert st.engine is not None
            assert st.engine.running is True
        finally:
            await cap.teardown()

    async def test_teardown_stops_engine(self, configured: Path) -> None:
        from arcagent.modules.scheduler.capabilities import Scheduler

        st = _runtime.state()
        st.agent_run_fn = AsyncMock(return_value="ok")

        cap = Scheduler()
        await cap.setup(None)
        engine = st.engine
        assert engine is not None
        await cap.teardown()

        # Engine cleared on teardown; running flag flipped off.
        assert st.engine is None
        assert engine.running is False

    async def test_setup_is_idempotent(self, configured: Path) -> None:
        from arcagent.modules.scheduler.capabilities import Scheduler

        st = _runtime.state()
        st.agent_run_fn = AsyncMock(return_value="ok")

        cap = Scheduler()
        try:
            await cap.setup(None)
            first_engine = st.engine
            await cap.setup(None)  # second call: must not replace engine
            assert st.engine is first_engine
        finally:
            await cap.teardown()

    async def test_teardown_drains_in_flight_worker(self, configured: Path) -> None:
        """Engine.stop() cancels worker without orphaning the task."""
        from arcagent.modules.scheduler.capabilities import Scheduler

        st = _runtime.state()
        st.agent_run_fn = AsyncMock(return_value="ok")

        cap = Scheduler()
        await cap.setup(None)
        engine = st.engine
        assert engine is not None
        worker = engine._worker_task
        timer = engine._timer_task
        assert worker is not None
        assert timer is not None

        await cap.teardown()

        # Both background tasks should be done after teardown.
        assert worker.done()
        assert timer.done()


@pytest.mark.asyncio
class TestAgentReadyHook:
    async def test_binds_run_fn_to_engine(self, configured: Path) -> None:
        from arcagent.modules.scheduler.capabilities import (
            Scheduler,
            bind_agent_run_fn,
        )

        cap = Scheduler()
        await cap.setup(None)
        try:
            new_fn = AsyncMock(return_value="hello")
            ctx = SimpleNamespace(data={"run_fn": new_fn})
            await bind_agent_run_fn(ctx)

            st = _runtime.state()
            assert st.agent_run_fn is new_fn
            assert st.engine is not None
            assert st.engine._agent_run_fn is new_fn
        finally:
            await cap.teardown()

    async def test_skips_when_no_run_fn(self, configured: Path) -> None:
        from arcagent.modules.scheduler.capabilities import bind_agent_run_fn

        ctx = SimpleNamespace(data={})
        await bind_agent_run_fn(ctx)  # must not raise

        st = _runtime.state()
        assert st.agent_run_fn is None


@pytest.mark.asyncio
class TestRuntimeContract:
    async def test_unconfigured_raises(self) -> None:
        from arcagent.modules.scheduler.capabilities import schedule_list

        with pytest.raises(RuntimeError, match="before runtime is configured"):
            await schedule_list()


@pytest.mark.asyncio
class TestCrudTools:
    async def test_schedule_create_and_list(self, configured: Path) -> None:
        import json

        from arcagent.modules.scheduler.capabilities import (
            schedule_create,
            schedule_list,
        )

        result = await schedule_create(
            type="interval",
            prompt="Heartbeat check",
            every_seconds=300,
        )
        created = json.loads(result)
        assert "id" in created
        assert created["prompt"] == "Heartbeat check"

        listed = json.loads(await schedule_list())
        assert len(listed) == 1
        assert listed[0]["id"] == created["id"]

    async def test_schedule_update(self, configured: Path) -> None:
        import json

        from arcagent.modules.scheduler.capabilities import (
            schedule_create,
            schedule_update,
        )

        created = json.loads(
            await schedule_create(
                type="interval",
                prompt="Initial",
                every_seconds=300,
            )
        )
        updated_raw = await schedule_update(id=created["id"], prompt="Renamed")
        updated = json.loads(updated_raw)
        assert updated["prompt"] == "Renamed"

    async def test_schedule_cancel(self, configured: Path) -> None:
        import json

        from arcagent.modules.scheduler.capabilities import (
            schedule_cancel,
            schedule_create,
        )

        created = json.loads(
            await schedule_create(
                type="interval",
                prompt="To cancel",
                every_seconds=300,
            )
        )
        result = json.loads(await schedule_cancel(id=created["id"]))
        assert result["status"] == "disabled"

        deleted = json.loads(await schedule_cancel(id=created["id"], delete=True))
        assert deleted["status"] == "deleted"
