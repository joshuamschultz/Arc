"""Tests for ReAct strategy loop."""

import asyncio
import time

import pytest
from conftest import LLMResponse, Message, MockModel, ToolCall

from arcrun.events import EventBus
from arcrun.registry import ToolRegistry
from arcrun.sandbox import Sandbox
from arcrun.state import RunState
from arcrun.strategies.react import react_loop
from arcrun.types import SandboxConfig, Tool


async def _echo_execute(params: dict, ctx: object) -> str:
    return f"echo: {params.get('input', '')}"


async def _failing_execute(params: dict, ctx: object) -> str:
    raise ValueError("tool exploded")


def _make_state(
    bus: EventBus,
    tools: list[Tool] | None = None,
    transform_context=None,
) -> RunState:
    if tools is None:
        tools = [
            Tool(
                name="echo",
                description="Echo input",
                input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
                execute=_echo_execute,
            )
        ]
    reg = ToolRegistry(tools=tools, event_bus=bus)
    messages = [
        Message(role="system", content="You are helpful."),
        Message(role="user", content="Do the task."),
    ]
    return RunState(
        messages=messages,
        registry=reg,
        event_bus=bus,
        run_id="test-run",
        transform_context=transform_context,
    )


class TestReactLoop:
    @pytest.mark.asyncio
    async def test_single_turn_end_turn(self):
        bus = EventBus(run_id="test")
        state = _make_state(bus)
        model = MockModel([LLMResponse(content="All done.", stop_reason="end_turn")])
        sandbox = Sandbox(config=None, event_bus=bus)

        result = await react_loop(model, state, sandbox, max_turns=5)
        assert result.content == "All done."
        assert result.turns == 1
        assert result.strategy_used == "react"

    @pytest.mark.asyncio
    async def test_multi_turn_tool_call(self):
        bus = EventBus(run_id="test")
        state = _make_state(bus)
        model = MockModel(
            [
                LLMResponse(
                    content=None,
                    tool_calls=[ToolCall(id="tc1", name="echo", arguments={"input": "hello"})],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="Got the echo result.", stop_reason="end_turn"),
            ]
        )
        sandbox = Sandbox(config=None, event_bus=bus)

        result = await react_loop(model, state, sandbox, max_turns=5)
        assert result.content == "Got the echo result."
        assert result.turns == 2
        assert result.tool_calls_made == 1

    @pytest.mark.asyncio
    async def test_tool_denied_by_sandbox(self):
        bus = EventBus(run_id="test")
        state = _make_state(bus)
        cfg = SandboxConfig(allowed_tools=["other_tool"])
        sandbox = Sandbox(config=cfg, event_bus=bus)

        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[ToolCall(id="tc1", name="echo", arguments={"input": "x"})],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="OK denied.", stop_reason="end_turn"),
            ]
        )

        result = await react_loop(model, state, sandbox, max_turns=5)
        denied = [e for e in bus.events if e.type == "tool.denied"]
        assert len(denied) == 1
        assert result.content == "OK denied."

    @pytest.mark.asyncio
    async def test_tool_exception_caught(self):
        bus = EventBus(run_id="test")
        fail_tool = Tool(
            name="fail",
            description="Always fails",
            input_schema={"type": "object"},
            execute=_failing_execute,
        )
        state = _make_state(bus, tools=[fail_tool])
        sandbox = Sandbox(config=None, event_bus=bus)

        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[ToolCall(id="tc1", name="fail", arguments={})],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="Handled error.", stop_reason="end_turn"),
            ]
        )

        result = await react_loop(model, state, sandbox, max_turns=5)
        error_events = [e for e in bus.events if e.type == "tool.error"]
        assert len(error_events) == 1
        assert "tool exploded" in error_events[0].data["error"]
        assert result.content == "Handled error."

    @pytest.mark.asyncio
    async def test_param_validation_failure(self):
        bus = EventBus(run_id="test")
        strict_tool = Tool(
            name="strict",
            description="Strict schema",
            input_schema={
                "type": "object",
                "properties": {"count": {"type": "integer"}},
                "required": ["count"],
            },
            execute=_echo_execute,
        )
        state = _make_state(bus, tools=[strict_tool])
        sandbox = Sandbox(config=None, event_bus=bus)

        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[ToolCall(id="tc1", name="strict", arguments={"count": "not_int"})],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="Fixed it.", stop_reason="end_turn"),
            ]
        )

        result = await react_loop(model, state, sandbox, max_turns=5)
        assert result.content == "Fixed it."

    @pytest.mark.asyncio
    async def test_max_turns_hit(self):
        bus = EventBus(run_id="test")
        state = _make_state(bus)
        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[ToolCall(id=f"tc{i}", name="echo", arguments={"input": "x"})],
                    stop_reason="tool_use",
                )
                for i in range(5)
            ]
        )
        sandbox = Sandbox(config=None, event_bus=bus)

        result = await react_loop(model, state, sandbox, max_turns=2)
        assert result.turns == 2
        max_events = [e for e in bus.events if e.type == "loop.max_turns"]
        assert len(max_events) == 1

    @pytest.mark.asyncio
    async def test_text_and_tool_calls_preserved(self):
        bus = EventBus(run_id="test")
        state = _make_state(bus)
        model = MockModel(
            [
                LLMResponse(
                    content="Let me think...",
                    tool_calls=[ToolCall(id="tc1", name="echo", arguments={"input": "y"})],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="Done.", stop_reason="end_turn"),
            ]
        )
        sandbox = Sandbox(config=None, event_bus=bus)

        result = await react_loop(model, state, sandbox, max_turns=5)
        assert result.content == "Done."
        assert result.tool_calls_made == 1

    @pytest.mark.asyncio
    async def test_all_core_events_emitted(self):
        bus = EventBus(run_id="test")
        state = _make_state(bus)
        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[ToolCall(id="tc1", name="echo", arguments={"input": "hi"})],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="Done.", stop_reason="end_turn"),
            ]
        )
        sandbox = Sandbox(config=None, event_bus=bus)

        await react_loop(model, state, sandbox, max_turns=5)
        event_types = [e.type for e in bus.events]
        assert "loop.start" in event_types
        assert "turn.start" in event_types
        assert "llm.call" in event_types
        assert "tool.start" in event_types
        assert "tool.end" in event_types
        assert "turn.end" in event_types
        assert "loop.complete" in event_types

    @pytest.mark.asyncio
    async def test_transform_context_called(self):
        bus = EventBus(run_id="test")
        calls = []

        def transform(messages):
            calls.append(len(messages))
            return messages

        state = _make_state(bus, transform_context=transform)
        model = MockModel([LLMResponse(content="OK.", stop_reason="end_turn")])
        sandbox = Sandbox(config=None, event_bus=bus)

        await react_loop(model, state, sandbox, max_turns=5)
        assert len(calls) >= 1


class TestParallelSafeDispatch:
    """Tools with parallel_safe=True dispatch concurrently in one turn."""

    @pytest.mark.asyncio
    async def test_parallel_safe_calls_run_concurrently(self):
        async def slow(params: dict, ctx: object) -> str:
            await asyncio.sleep(0.1)
            return f"ok:{params['n']}"

        tool = Tool(
            name="slow",
            description="slow tool",
            input_schema={
                "type": "object",
                "properties": {"n": {"type": "integer"}},
                "required": ["n"],
            },
            execute=slow,
            parallel_safe=True,
        )
        bus = EventBus(run_id="test")
        state = _make_state(bus, tools=[tool])
        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(id=f"tc{i}", name="slow", arguments={"n": i})
                        for i in range(3)
                    ],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="all done", stop_reason="end_turn"),
            ]
        )
        sandbox = Sandbox(config=None, event_bus=bus)

        start = time.monotonic()
        result = await react_loop(model, state, sandbox, max_turns=5)
        elapsed = time.monotonic() - start

        # Sequential would be ~0.3s; parallel should be ~0.1s + overhead.
        assert elapsed < 0.25, f"expected parallel dispatch, took {elapsed:.3f}s"
        assert result.content == "all done"

    @pytest.mark.asyncio
    async def test_non_parallel_safe_calls_run_serially(self):
        async def slow(params: dict, ctx: object) -> str:
            await asyncio.sleep(0.05)
            return f"ok:{params['n']}"

        tool = Tool(
            name="slow",
            description="slow tool",
            input_schema={
                "type": "object",
                "properties": {"n": {"type": "integer"}},
                "required": ["n"],
            },
            execute=slow,
            parallel_safe=False,
        )
        bus = EventBus(run_id="test")
        state = _make_state(bus, tools=[tool])
        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(id=f"tc{i}", name="slow", arguments={"n": i})
                        for i in range(3)
                    ],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="all done", stop_reason="end_turn"),
            ]
        )
        sandbox = Sandbox(config=None, event_bus=bus)

        start = time.monotonic()
        await react_loop(model, state, sandbox, max_turns=5)
        elapsed = time.monotonic() - start

        # Sequential ~0.15s; parallel would be ~0.05s.
        assert elapsed >= 0.12, f"expected sequential dispatch, took {elapsed:.3f}s"


class TestSignalsCompletion:
    """Tools with signals_completion=True terminate the loop with their args."""

    @pytest.mark.asyncio
    async def test_signals_completion_terminates_with_payload(self):
        async def finish(params: dict, ctx: object) -> str:
            return f"completed:{params.get('summary', '')}"

        terminator = Tool(
            name="my_finish",
            description="generic terminator",
            input_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["status", "summary"],
            },
            execute=finish,
            signals_completion=True,
        )
        bus = EventBus(run_id="test")
        state = _make_state(bus, tools=[terminator])
        # Second response would only fire if loop did NOT terminate.
        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="tc1",
                            name="my_finish",
                            arguments={"status": "success", "summary": "got it"},
                        )
                    ],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="should not be reached", stop_reason="end_turn"),
            ]
        )
        sandbox = Sandbox(config=None, event_bus=bus)

        result = await react_loop(model, state, sandbox, max_turns=5)

        assert state.completion_payload is not None
        assert state.completion_payload["status"] == "success"
        assert state.completion_payload["summary"] == "got it"
        # Loop terminated with the tool's summary, not the would-be next turn.
        assert result.content == "got it"
        assert model._call_count == 1, "loop should have terminated after first turn"

    @pytest.mark.asyncio
    async def test_no_completion_when_flag_unset(self):
        async def noop(params: dict, ctx: object) -> str:
            return "ok"

        regular = Tool(
            name="regular",
            description="d",
            input_schema={"type": "object"},
            execute=noop,
            signals_completion=False,
        )
        bus = EventBus(run_id="test")
        state = _make_state(bus, tools=[regular])
        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[ToolCall(id="tc1", name="regular", arguments={})],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="continued", stop_reason="end_turn"),
            ]
        )
        sandbox = Sandbox(config=None, event_bus=bus)

        result = await react_loop(model, state, sandbox, max_turns=5)
        assert state.completion_payload is None
        assert result.content == "continued"
