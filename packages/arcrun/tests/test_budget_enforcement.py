"""SPEC-038 Phase A — per-run token+cost circuit-breaker.

Token is the primary ceiling (present on both streaming and non-streaming
paths); cost is the best-effort secondary. Both breach sites route through
``make_budget_breach_args`` (no inline payload dicts).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class _ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


@dataclass
class _ModelResponse:
    content: str = ""
    stop_reason: str = "tool_use"
    tool_calls: list[_ToolCall] | None = None
    cost_usd: float = 0.0
    usage: _Usage | None = None

    def __post_init__(self) -> None:
        if self.tool_calls is None:
            self.tool_calls = []


class _StubModel:
    def __init__(self, responses: list[_ModelResponse]) -> None:
        self._responses = list(responses)
        self._idx = 0

    async def invoke(self, _messages: Any, tools: Any = None, **_: Any) -> Any:
        if self._idx >= len(self._responses):
            return _ModelResponse(stop_reason="end_turn")
        resp = self._responses[self._idx]
        self._idx += 1
        return resp


def _make_state(**overrides: Any) -> Any:
    from arcrun.builtins.task_complete import make_task_complete_tool
    from arcrun.events import EventBus
    from arcrun.registry import ToolRegistry
    from arcrun.state import RunState

    bus = EventBus(run_id="test-run")
    registry = ToolRegistry([make_task_complete_tool()], bus)
    state = RunState(messages=[], registry=registry, event_bus=bus, run_id="test-run")
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


async def _run_loop(state: Any, model: _StubModel, *, max_turns: int = 10) -> Any:
    from arcrun.sandbox import Sandbox
    from arcrun.strategies.react import react_loop
    from arcrun.types import SandboxConfig

    sandbox = Sandbox(SandboxConfig(), state.event_bus)
    return await react_loop(model, state, sandbox, max_turns=max_turns)


class TestRunStateBudgetFields:
    def test_run_state_carries_max_tokens_and_max_cost(self) -> None:
        state = _make_state(max_tokens=100, max_cost_usd=1.0)
        assert state.max_tokens == 100
        assert state.max_cost_usd == 1.0

    def test_dead_budget_fields_removed(self) -> None:
        from arcrun.state import RunState

        names = {f.name for f in RunState.__dataclass_fields__.values()}
        assert "token_budget" not in names
        assert "cost_budget" not in names
        assert "max_tokens" in names


class TestTokenCircuitBreaker:
    async def test_token_breach_halts_before_next_turn(self) -> None:
        model = _StubModel(
            [
                _ModelResponse(
                    stop_reason="tool_use",
                    usage=_Usage(input_tokens=60, output_tokens=40, total_tokens=100),
                    tool_calls=[_ToolCall(id="tc1", name="nonexistent", arguments={})],
                ),
                _ModelResponse(stop_reason="end_turn", content="should-not-happen"),
            ]
        )
        state = _make_state(max_tokens=80)
        result = await _run_loop(state, model, max_turns=10)

        assert state.completion_payload is not None
        assert state.completion_payload["error"] == "max_tokens"
        assert result.turns == 1

    async def test_token_breach_emits_loop_completed_with_usage(self) -> None:
        model = _StubModel(
            [
                _ModelResponse(
                    stop_reason="tool_use",
                    usage=_Usage(input_tokens=60, output_tokens=40, total_tokens=100),
                    tool_calls=[_ToolCall(id="tc1", name="nonexistent", arguments={})],
                ),
            ]
        )
        state = _make_state(max_tokens=80)
        await _run_loop(state, model, max_turns=10)

        completed = [e for e in state.event_bus.events if e.type == "loop.completed"]
        assert completed
        payload = completed[0].data
        assert payload["reason"] == "max_tokens"
        assert payload["tokens"]["total"] == 100


class TestBreachArgsHelper:
    def test_helper_supports_max_tokens(self) -> None:
        from arcrun.builtins.task_complete import make_budget_breach_args

        args = make_budget_breach_args(reason="max_tokens")
        assert args.status == "failed"
        assert args.error == "max_tokens"
