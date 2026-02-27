"""Tests for ReAct strategy loop."""

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
