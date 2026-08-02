"""Shared test fixtures — mock arcllm types."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import pytest

_IN_CI = os.environ.get("CI", "").lower() not in ("", "0", "false")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Gate ``requires_docker`` on a reachable *daemon*, not on the CLI existing.

    ``shutil.which("docker")`` was the old predicate and it lied: OrbStack and
    Docker Desktop leave the CLI on PATH while the daemon is stopped, so the
    isolation suites ran anyway and died on a socket error.

    Locally a stopped daemon skips. In CI it does NOT skip — the tests run and
    fail the build, because these suites are the only evidence that isolation
    holds, and a security suite that silently passes without exercising anything
    is worse than one that is red.
    """
    if _IN_CI:
        return
    from arcrun.backends import DockerBackend

    if DockerBackend.available():
        return
    skip = pytest.mark.skip(reason="no reachable Docker daemon (required in CI)")
    for item in items:
        if "requires_docker" in item.keywords:
            item.add_marker(skip)


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
        self.invoke_calls: list[dict[str, Any]] = []

    async def invoke(self, messages: list[Any], tools: list[Any] | None = None) -> LLMResponse:
        self.invoke_calls.append({"messages": messages, "tools": tools})
        if self._call_count >= len(self._responses):
            raise RuntimeError("MockModel exhausted responses")
        resp = self._responses[self._call_count]
        self._call_count += 1
        return resp
