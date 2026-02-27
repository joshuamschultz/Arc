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
