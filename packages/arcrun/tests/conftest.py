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
    """Mock arcllm model that returns predetermined responses.

    Strategy selection is answered separately and does **not** consume a scripted
    response, a scripted call count, or scripted spend. Every run now opens with a
    selection call, and letting it eat the first scripted turn would mean every
    test in the suite had to script a reply it does not care about — which would
    say nothing about the behaviour under test. The same holds for its usage: an
    answer this fixture invented must not add tokens or cost the test never wrote,
    or every budget assertion silently measures the fixture instead of the run.
    Tests that are *about* selection drive it explicitly instead, and their
    scripted response carries its own real usage (``test_strategy_selection.py``).
    """

    def __init__(self, responses: list[LLMResponse], *, strategy: str = "react") -> None:
        self._responses = list(responses)
        self._call_count = 0
        self._strategy = strategy
        self.invoke_calls: list[dict[str, Any]] = []
        self.selection_calls = 0

    def _scripts_selection(self) -> bool:
        """True when the test is driving selection itself, so do not answer for it."""
        if self._call_count >= len(self._responses):
            return False
        calls = self._responses[self._call_count].tool_calls
        return bool(calls) and calls[0].name == "select_strategy"

    async def invoke(self, messages: list[Any], tools: list[Any] | None = None) -> LLMResponse:
        self.invoke_calls.append({"messages": messages, "tools": tools})
        if _is_strategy_selection(tools) and not self._scripts_selection():
            self.selection_calls += 1
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="select-strategy",
                        name="select_strategy",
                        arguments={"strategy": self._strategy},
                    )
                ],
                usage=Usage(input_tokens=0, output_tokens=0, total_tokens=0),
                cost_usd=0.0,
            )
        if self._call_count >= len(self._responses):
            raise RuntimeError("MockModel exhausted responses")
        resp = self._responses[self._call_count]
        self._call_count += 1
        return resp

    @property
    def task_calls(self) -> list[dict[str, Any]]:
        """Invocations that did the actual work, with selection filtered out.

        Every run now opens by asking which strategy to use, so ``invoke_calls[0]``
        is no longer the first turn of the task. A test about the task should say
        so rather than counting past a call it does not care about.
        """
        return [c for c in self.invoke_calls if not _is_strategy_selection(c["tools"])]


def _is_strategy_selection(tools: list[Any] | None) -> bool:
    """True when this call is the loop asking which strategy to run."""
    return bool(tools) and any(getattr(t, "name", "") == "select_strategy" for t in tools or [])
