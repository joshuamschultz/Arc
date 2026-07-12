"""SPEC-056 Phase B — ``tasks`` module capability-loader registration — RED.

Mirrors ``test_scheduler_capabilities.py::TestLoaderRegistration``: the loader
scans ``capabilities.py`` and must register every ``@tool``-stamped function as
a :class:`ToolEntry`. The tasks module has no lifecycle engine (no background
loop, unlike scheduler) so there is no ``@capability`` class to assert on here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcagent.capabilities.capability_loader import CapabilityLoader
from arcagent.capabilities.capability_registry import CapabilityRegistry, ToolEntry


@pytest.mark.asyncio
class TestLoaderRegistration:
    async def test_all_task_tools_register(self) -> None:
        from arcagent.modules.tasks import capabilities as tasks_caps

        module_dir = Path(tasks_caps.__file__).parent
        reg = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=[("tasks", module_dir)], registry=reg)
        await loader.scan_and_register()

        for tool_name in (
            "create_task",
            "update_task",
            "start_task",
            "complete_task",
            "fail_task",
            "assign_task",
            "claim_task",
            "list_tasks",
            "decompose_task",
            "set_task_output",
        ):
            entry = await reg.get_tool(tool_name)
            assert entry is not None, f"missing tool {tool_name}"
            assert isinstance(entry, ToolEntry)

    async def test_state_modifying_tools_classified_correctly(self) -> None:
        from arcagent.modules.tasks import capabilities as tasks_caps

        module_dir = Path(tasks_caps.__file__).parent
        reg = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=[("tasks", module_dir)], registry=reg)
        await loader.scan_and_register()

        read_only = {"list_tasks"}
        for tool_name in read_only:
            entry = await reg.get_tool(tool_name)
            assert entry is not None
            assert entry.meta.classification == "read_only"

        for tool_name in (
            "create_task",
            "update_task",
            "start_task",
            "complete_task",
            "fail_task",
            "assign_task",
            "claim_task",
            "decompose_task",
            "set_task_output",
        ):
            entry = await reg.get_tool(tool_name)
            assert entry is not None
            assert entry.meta.classification == "state_modifying"

    async def test_dispatch_loop_and_bind_hook_register(self) -> None:
        # Phase D producers must be discovered by the real loader — the
        # dispatch background task and the agent:ready run-fn bind hook — or
        # assigned tasks silently never run (the producers-unwired failure mode).
        from arcagent.modules.tasks import capabilities as tasks_caps

        module_dir = Path(tasks_caps.__file__).parent
        reg = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=[("tasks", module_dir)], registry=reg)
        await loader.scan_and_register()

        task_entry = await reg.get_task("tasks_dispatch_loop")
        assert task_entry is not None, "dispatch loop not registered by loader"

        ready_hooks = await reg.get_hooks("agent:ready")
        assert any(
            h.meta.name == "tasks_bind_run_fn" for h in ready_hooks
        ), "agent:ready run-fn bind hook not registered by loader"
