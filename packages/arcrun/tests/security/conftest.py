"""Shared fixtures for security tests."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from arcrun.events import EventBus
from arcrun.types import SandboxConfig, Tool, ToolContext


@dataclass
class Usage:
    input_tokens: int = 10
    output_tokens: int = 5
    total_tokens: int = 15


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.001


class MockModel:
    """Mock arcllm model with predetermined responses."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self._call_count = 0
        self.invoke_calls: list[dict] = []

    async def invoke(self, messages: list, tools: list | None = None) -> LLMResponse:
        self.invoke_calls.append({"messages": messages, "tools": tools})
        if self._call_count >= len(self._responses):
            raise RuntimeError("MockModel exhausted responses")
        resp = self._responses[self._call_count]
        self._call_count += 1
        return resp


async def noop_execute(params: dict[str, Any], ctx: ToolContext) -> str:
    return "ok"


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
