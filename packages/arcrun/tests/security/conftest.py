"""Shared fixtures for security tests."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

# The model fakes live in the parent conftest and are re-exported here.
# They were duplicated once, and the copy silently rotted: when every run
# started opening with a strategy-selection call, the parent copy was taught
# about it and this one was not, which desynced every scripted response in
# the security suite. One definition, imported, cannot drift like that.
from packages.arcrun.tests.conftest import (
    LLMResponse,
    MockModel,
    ToolCall,
    Usage,
)

from arcrun.events import EventBus
from arcrun.types import SandboxConfig, Tool, ToolContext

__all__ = ["LLMResponse", "MockModel", "ToolCall", "Usage", "make_ctx"]


@pytest.fixture
def event_bus():
    return EventBus(run_id="security-test")


@pytest.fixture
def echo_tool():
    async def _echo(params: dict[str, Any], ctx: ToolContext) -> str:
        return f"echo: {params.get('input', '')}"

    return Tool(
        name="echo",
        description="Echo input",
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
        execute=_echo,
    )


@pytest.fixture
def restrictive_sandbox():
    return SandboxConfig(allowed_tools=["echo"])


@pytest.fixture
def permissive_sandbox():
    return SandboxConfig()


def make_ctx(run_id: str = "test-run") -> ToolContext:
    return ToolContext(
        run_id=run_id,
        tool_call_id="tc-1",
        turn_number=0,
        event_bus=None,
        cancelled=asyncio.Event(),
    )
