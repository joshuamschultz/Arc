"""Unit tests for planning module lifecycle."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from arcagent.modules.planning import PlanningModule


def _make_module(tmp_path: Path) -> PlanningModule:
    """Create a PlanningModule with test config."""
    return PlanningModule(
        config={"enabled": True},
        workspace=tmp_path,
    )


def _make_ctx(tmp_path: Path) -> MagicMock:
    """Create a mock ModuleContext for startup tests."""
    ctx = MagicMock()
    ctx.bus = MagicMock()
    ctx.bus.subscribe = MagicMock()
    ctx.tool_registry = MagicMock()
    ctx.tool_registry.register = MagicMock()
    ctx.workspace = tmp_path
    return ctx


class TestModuleProtocol:
    def test_has_name(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        assert module.name == "planning"

    def test_has_startup(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        assert callable(module.startup)

    def test_has_shutdown(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        assert callable(module.shutdown)


class TestModuleStartup:
    @pytest.mark.asyncio
    async def test_startup_registers_four_tools(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx = _make_ctx(tmp_path)
        await module.startup(ctx)
        assert ctx.tool_registry.register.call_count == 4

    @pytest.mark.asyncio
    async def test_startup_subscribes_to_events(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx = _make_ctx(tmp_path)
        await module.startup(ctx)
        subscribed = [call.args[0] for call in ctx.bus.subscribe.call_args_list]
        assert "agent:assemble_prompt" in subscribed
        assert "agent:shutdown" in subscribed


class TestPendingTasks:
    def test_no_tasks_file_returns_empty(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        assert module._load_pending_tasks() == []

    def test_loads_pending_tasks(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        tasks = [
            {"id": "task_1", "description": "Do something", "status": "waiting"},
            {"id": "task_2", "description": "Done task", "status": "done"},
        ]
        (tmp_path / "tasks.json").write_text(json.dumps(tasks), encoding="utf-8")
        result = module._load_pending_tasks()
        assert len(result) == 1
        assert result[0]["description"] == "Do something"

    def test_invalid_json_returns_empty(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        (tmp_path / "tasks.json").write_text("not valid json", encoding="utf-8")
        assert module._load_pending_tasks() == []
