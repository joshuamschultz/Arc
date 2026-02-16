"""Shared fixtures for scheduler tests — SPEC-002."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from arcagent.modules.scheduler.models import ScheduleEntry
from arcagent.modules.scheduler.store import ScheduleStore
from arcagent.modules.scheduler.tools import create_scheduler_tools


def make_config() -> MagicMock:
    """Create a mock SchedulerConfig with all required fields."""
    cfg = MagicMock()
    cfg.min_interval_seconds = 60
    cfg.max_schedules = 50
    cfg.max_prompt_length = 500
    cfg.default_timeout_seconds = 300
    cfg.max_timeout_seconds = 3600
    cfg.circuit_breaker_threshold = 3
    cfg.check_interval_seconds = 30
    cfg.store_path = "schedules.json"
    cfg.enabled = True
    return cfg


def make_entry(
    id: str = "sched_test",  # noqa: A002 - matches ScheduleEntry field
    type: str = "interval",  # noqa: A002 - matches ScheduleEntry field
    prompt: str = "Heartbeat",
    every_seconds: int = 300,
    enabled: bool = True,
    **kwargs: Any,
) -> ScheduleEntry:
    """Create a valid ScheduleEntry for tests."""
    return ScheduleEntry(
        id=id, type=type, prompt=prompt, every_seconds=every_seconds,
        enabled=enabled, **kwargs,
    )


def make_ctx(tmp_path: Path) -> MagicMock:
    """Create a mock ModuleContext."""
    ctx = MagicMock()
    ctx.bus = MagicMock()
    ctx.bus.subscribe = MagicMock()
    ctx.tool_registry = MagicMock()
    ctx.tool_registry.register = MagicMock()
    ctx.workspace = tmp_path
    ctx.agent_run_fn = AsyncMock(return_value="ok")
    return ctx


def setup_tools(tmp_path: Path) -> tuple[list[Any], ScheduleStore]:
    """Create tools with a real store backed by tmp_path."""
    store = ScheduleStore(tmp_path / "schedules.json")
    config = make_config()
    telemetry = MagicMock()
    tools = create_scheduler_tools(store=store, config=config, telemetry=telemetry)
    return tools, store


def find_tool(tools: list[Any], name: str) -> Any:
    """Find a tool by name."""
    for tool in tools:
        if tool.name == name:
            return tool
    msg = f"Tool '{name}' not found"
    raise ValueError(msg)
