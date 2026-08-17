"""Shared test fixtures — mock arcllm types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: str


@dataclass
class Message:
    role: str
    content: Any  # str or list of blocks


@dataclass
class LLMResponse:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.001


class MockModel:
    """Mock arcllm model that returns predetermined responses."""

    def __init__(self, responses: list[LLMResponse], *, strategy: str = "react") -> None:
        self._responses = list(responses)
        self._call_count = 0
        self._strategy = strategy
        self.invoke_calls: list[dict] = []
        self.selection_calls = 0

    def _scripts_selection(self) -> bool:
        """True when the test drives selection itself, so do not answer for it."""
        if self._call_count >= len(self._responses):
            return False
        calls = self._responses[self._call_count].tool_calls
        return bool(calls) and calls[0].name == "select_strategy"

    @property
    def task_calls(self) -> list[dict]:
        """Invocations that did real work, with strategy selection filtered out."""
        return [c for c in self.invoke_calls if not _is_strategy_selection(c["tools"])]

    async def invoke(self, messages: list, tools: list | None = None) -> LLMResponse:
        self.invoke_calls.append({"messages": messages, "tools": tools})
        if _is_strategy_selection(tools) and not self._scripts_selection():
            # Every run now opens by asking which strategy fits. Answering it
            # here rather than consuming a scripted response keeps each test
            # about the behaviour it was written for.
            self.selection_calls += 1
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="select-strategy",
                        name="select_strategy",
                        arguments={"strategy": self._strategy},
                    )
                ]
            )
        if self._call_count >= len(self._responses):
            raise RuntimeError("MockModel exhausted responses")
        resp = self._responses[self._call_count]
        self._call_count += 1
        return resp


def setup_spawn_tools(
    model,
    base_tools,
    system_prompt,
    *,
    sandbox=None,
    allowed_strategies=None,
    max_concurrent: int = 5,
    timeout_seconds: int = 300,
):
    """Test helper — see integration/orchestration/_mock_llm.py for docs."""
    from arcagent.orchestration import make_spawn_tool

    tools = list(base_tools)
    spawn_tool = make_spawn_tool(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        sandbox=sandbox,
        allowed_strategies=allowed_strategies,
        spawn_timeout_seconds=timeout_seconds,
        max_concurrent_spawns=max_concurrent,
    )
    tools.append(spawn_tool)
    return tools


def _is_strategy_selection(tools: list | None) -> bool:
    """True when this call is the loop asking which strategy to run."""
    return bool(tools) and any(getattr(t, "name", "") == "select_strategy" for t in tools or [])
