"""SPEC-043 Phase C — unified circuit breaker (runaway / error-cascade / turns)."""

from __future__ import annotations

import pytest
from conftest import LLMResponse, Message, MockModel, ToolCall

from arcrun.events import EventBus
from arcrun.registry import ToolRegistry
from arcrun.sandbox import Sandbox
from arcrun.state import RunState
from arcrun.strategies.react import check_breaker, react_loop
from arcrun.types import Tool


async def _echo(params: dict, ctx: object) -> str:
    return "ok"


async def _boom(params: dict, ctx: object) -> str:
    raise ValueError("boom")


def _tools() -> list[Tool]:
    return [
        Tool(
            name="echo",
            description="Echo",
            input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
            execute=_echo,
        ),
        Tool(
            name="boom",
            description="Always fails",
            input_schema={"type": "object", "properties": {}},
            execute=_boom,
        ),
    ]


def _state(bus: EventBus, **kw: object) -> RunState:
    reg = ToolRegistry(tools=_tools(), event_bus=bus)
    state = RunState(
        messages=[Message(role="user", content="go")],
        registry=reg,
        event_bus=bus,
        run_id="run",
    )
    for k, v in kw.items():
        setattr(state, k, v)
    return state


def _repeat(name: str, args: dict, n: int) -> list[LLMResponse]:
    return [
        LLMResponse(
            tool_calls=[ToolCall(id=f"c{i}", name=name, arguments=args)],
            stop_reason="tool_use",
        )
        for i in range(n)
    ]


class TestCheckBreakerUnit:
    def test_max_turns_via_check_breaker(self) -> None:
        bus = EventBus(run_id="t")
        state = _state(bus, max_turns=3, turn_count=3)
        assert check_breaker(state) == "max_turns"

    def test_runaway_reason(self) -> None:
        bus = EventBus(run_id="t")
        state = _state(bus, max_repeat=3, runaway_count=3)
        assert check_breaker(state) == "runaway_loop"

    def test_cascade_reason(self) -> None:
        bus = EventBus(run_id="t")
        state = _state(bus, max_consecutive_errors=2, consecutive_tool_errors=2)
        assert check_breaker(state) == "error_cascade"

    def test_no_breach(self) -> None:
        bus = EventBus(run_id="t")
        state = _state(bus, max_turns=25)
        assert check_breaker(state) is None


class TestRunawayLoop:
    @pytest.mark.asyncio
    async def test_repeated_identical_call_trips_runaway(self) -> None:
        bus = EventBus(run_id="t")
        state = _state(bus, max_repeat=3)
        model = MockModel(_repeat("echo", {"input": "x"}, 10))
        sandbox = Sandbox(config=None, event_bus=bus)
        result = await react_loop(model, state, sandbox, max_turns=25)
        assert result.completion_payload is not None
        assert result.completion_payload["error"] == "runaway_loop"
        completed = [e for e in bus.events if e.type == "loop.completed"]
        assert completed[-1].data["reason"] == "runaway_loop"

    @pytest.mark.asyncio
    async def test_distinct_parallel_batch_is_progress(self) -> None:
        """A batch of distinct signatures does NOT trip runaway (REQ-025)."""
        bus = EventBus(run_id="t")
        state = _state(bus, max_repeat=2)
        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(id="a", name="echo", arguments={"input": "1"}),
                        ToolCall(id="b", name="echo", arguments={"input": "2"}),
                        ToolCall(id="c", name="echo", arguments={"input": "3"}),
                    ],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="done", stop_reason="end_turn"),
            ]
        )
        sandbox = Sandbox(config=None, event_bus=bus)
        result = await react_loop(model, state, sandbox, max_turns=25)
        assert result.content == "done"
        assert result.completion_payload is None


class TestErrorCascade:
    @pytest.mark.asyncio
    async def test_consecutive_failures_trip_cascade(self) -> None:
        bus = EventBus(run_id="t")
        state = _state(bus, max_consecutive_errors=3)
        # Each turn calls the always-failing tool with DISTINCT args so the
        # runaway detector does not fire first — we isolate the cascade breaker.
        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[ToolCall(id=f"c{i}", name="boom", arguments={"n": i})],
                    stop_reason="tool_use",
                )
                for i in range(10)
            ]
        )
        sandbox = Sandbox(config=None, event_bus=bus)
        result = await react_loop(model, state, sandbox, max_turns=25)
        assert result.completion_payload is not None
        assert result.completion_payload["error"] == "error_cascade"

    @pytest.mark.asyncio
    async def test_success_resets_cascade_counter(self) -> None:
        bus = EventBus(run_id="t")
        state = _state(bus, max_consecutive_errors=2)
        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[ToolCall(id="c0", name="boom", arguments={"n": 0})],
                    stop_reason="tool_use",
                ),
                LLMResponse(
                    tool_calls=[ToolCall(id="c1", name="echo", arguments={"input": "ok"})],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="done", stop_reason="end_turn"),
            ]
        )
        sandbox = Sandbox(config=None, event_bus=bus)
        result = await react_loop(model, state, sandbox, max_turns=25)
        assert result.content == "done"
        assert result.completion_payload is None
