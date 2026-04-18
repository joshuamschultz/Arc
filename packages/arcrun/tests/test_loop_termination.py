"""SPEC-017 Phase 5 — task_complete termination + budget caps.

End-to-end tests through the React strategy. Uses minimal stub
models / tools so the logic under test is the loop's handling of
``task_complete``, ``max_turns``, and ``max_cost_usd``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest


# --- Stubs ---------------------------------------------------------------


@dataclass
class _ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class _ModelResponse:
    content: str = ""
    stop_reason: str = "tool_use"
    tool_calls: list[_ToolCall] | None = None
    cost_usd: float = 0.0

    def __post_init__(self) -> None:
        if self.tool_calls is None:
            self.tool_calls = []


class _StubModel:
    """Replay a fixed sequence of responses turn-by-turn."""

    def __init__(self, responses: list[_ModelResponse]) -> None:
        self._responses = list(responses)
        self._idx = 0

    async def invoke(self, _messages: Any, tools: Any = None, **_: Any) -> Any:
        if self._idx >= len(self._responses):
            # Avoid infinite loops if the test under-specifies responses
            return _ModelResponse(stop_reason="end_turn")
        resp = self._responses[self._idx]
        self._idx += 1
        return resp


# --- Setup helpers -------------------------------------------------------


def _make_state(model_responses: list[_ModelResponse], **overrides: Any) -> Any:
    from arcrun.builtins.task_complete import make_task_complete_tool
    from arcrun.events import EventBus
    from arcrun.registry import ToolRegistry
    from arcrun.state import RunState

    bus = EventBus(run_id="test-run")
    registry = ToolRegistry([make_task_complete_tool()], bus)
    state = RunState(
        messages=[],
        registry=registry,
        event_bus=bus,
        run_id="test-run",
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


async def _run_loop(state: Any, model: _StubModel, *, max_turns: int = 10) -> Any:
    from arcrun.sandbox import Sandbox
    from arcrun.strategies.react import react_loop
    from arcrun.types import SandboxConfig

    sandbox = Sandbox(SandboxConfig(), state.event_bus)
    return await react_loop(model, state, sandbox, max_turns=max_turns)


# --- Tests --------------------------------------------------------------


class TestTaskCompleteTerminates:
    async def test_success_status_terminates_loop(self) -> None:
        model = _StubModel(
            [
                _ModelResponse(
                    stop_reason="tool_use",
                    tool_calls=[
                        _ToolCall(
                            id="tc1",
                            name="task_complete",
                            arguments={"status": "success", "summary": "done"},
                        )
                    ],
                ),
            ]
        )
        state = _make_state([])
        result = await _run_loop(state, model, max_turns=10)

        # Loop terminated after one turn with the completion payload
        assert state.completion_payload == {
            "status": "success",
            "summary": "done",
        }
        assert result.content == "done"
        assert result.turns == 1

    async def test_loop_completed_event_emitted(self) -> None:
        model = _StubModel(
            [
                _ModelResponse(
                    stop_reason="tool_use",
                    tool_calls=[
                        _ToolCall(
                            id="tc1",
                            name="task_complete",
                            arguments={"status": "partial", "summary": "some"},
                        )
                    ],
                ),
            ]
        )
        state = _make_state([])
        await _run_loop(state, model, max_turns=10)

        events = [e.type for e in state.event_bus.events]
        assert "loop.completed" in events


class TestMaxTurns:
    async def test_max_turns_synthesizes_failed_completion(self) -> None:
        """Model that never calls task_complete gets capped at max_turns."""
        model = _StubModel(
            [
                _ModelResponse(
                    stop_reason="tool_use",
                    tool_calls=[
                        _ToolCall(
                            id="tc1",
                            name="task_complete",
                            arguments={},  # invalid — forces retry
                        )
                    ],
                )
            ]
            * 20
        )
        # Actually we want a model that doesn't call task_complete at all
        model = _StubModel(
            [
                _ModelResponse(
                    stop_reason="end_turn", content="turn-1"
                ),  # no tool calls; this ends cleanly, not what we want
            ]
        )

        # Use a model that always returns end_turn with no tool_calls
        # to simulate an agent that never signals completion. The loop
        # should reach max_turns.
        class _NeverComplete:
            async def invoke(self, *_a: Any, **_kw: Any) -> Any:
                return _ModelResponse(
                    stop_reason="tool_use",
                    tool_calls=[
                        _ToolCall(
                            id=f"tc{id(object())}",
                            name="nonexistent",  # not task_complete
                            arguments={},
                        )
                    ],
                )

        state = _make_state([])
        await _run_loop(state, _NeverComplete(), max_turns=3)

        # Payload is set to failed/max_turns
        assert state.completion_payload is not None
        assert state.completion_payload["status"] == "failed"
        assert state.completion_payload["error"] == "max_turns"


class TestMaxCost:
    async def test_max_cost_breach_terminates_before_next_turn(self) -> None:
        model = _StubModel(
            [
                _ModelResponse(
                    stop_reason="tool_use",
                    cost_usd=5.0,
                    tool_calls=[
                        _ToolCall(
                            id="tc1",
                            name="nonexistent",
                            arguments={},
                        )
                    ],
                ),
                _ModelResponse(stop_reason="end_turn", content="should-not-happen"),
            ]
        )
        state = _make_state([], max_cost_usd=3.0)
        result = await _run_loop(state, model, max_turns=10)

        # After turn 1, cost_usd=5.0 ≥ 3.0 → cost cap kicks in pre-turn-2
        assert state.completion_payload is not None
        assert state.completion_payload["error"] == "max_cost"
        # Loop didn't progress to turn 2
        assert result.turns == 1


# asyncio auto-mode
pytestmark = pytest.mark.asyncio


_ = asyncio  # silence unused
