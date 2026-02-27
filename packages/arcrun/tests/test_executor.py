"""Tests for shared tool executor."""

import pytest
from conftest import Message, ToolCall

from arcrun.events import EventBus
from arcrun.registry import ToolRegistry
from arcrun.sandbox import Sandbox
from arcrun.state import RunState
from arcrun.types import SandboxConfig, Tool


async def _echo(params: dict, ctx: object) -> str:
    return f"echo: {params.get('input', '')}"


async def _explode(params: dict, ctx: object) -> str:
    raise RuntimeError("boom")


def _bus() -> EventBus:
    return EventBus(run_id="test")


def _state(bus: EventBus, tools: list[Tool] | None = None) -> RunState:
    if tools is None:
        tools = [
            Tool(
                name="echo",
                description="Echo input",
                input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
                execute=_echo,
            )
        ]
    return RunState(
        messages=[Message(role="user", content="go")],
        registry=ToolRegistry(tools=tools, event_bus=bus),
        event_bus=bus,
        run_id="test-run",
    )


class TestExecuteToolCall:
    @pytest.mark.asyncio
    async def test_happy_path(self):
        from arcrun.executor import execute_tool_call

        bus = _bus()
        state = _state(bus)
        sandbox = Sandbox(config=None, event_bus=bus)
        tc = ToolCall(id="tc1", name="echo", arguments={"input": "hi"})

        result_msg, ok = await execute_tool_call(tc, state, sandbox)
        assert ok is True
        assert state.tool_calls_made == 1
        # Result message should contain the echo output
        assert any("echo: hi" in str(block) for block in result_msg.content)

    @pytest.mark.asyncio
    async def test_sandbox_denied(self):
        from arcrun.executor import execute_tool_call

        bus = _bus()
        state = _state(bus)
        sandbox = Sandbox(config=SandboxConfig(allowed_tools=["other"]), event_bus=bus)
        tc = ToolCall(id="tc1", name="echo", arguments={"input": "x"})

        _result_msg, ok = await execute_tool_call(tc, state, sandbox)
        assert ok is False
        assert state.tool_calls_made == 0

    @pytest.mark.asyncio
    async def test_tool_not_found(self):
        from arcrun.executor import execute_tool_call

        bus = _bus()
        state = _state(bus)
        sandbox = Sandbox(config=None, event_bus=bus)
        tc = ToolCall(id="tc1", name="nonexistent", arguments={})

        _result_msg, ok = await execute_tool_call(tc, state, sandbox)
        assert ok is False
        assert state.tool_calls_made == 0

    @pytest.mark.asyncio
    async def test_schema_validation_failure(self):
        from arcrun.executor import execute_tool_call

        bus = _bus()
        strict = Tool(
            name="strict",
            description="Strict",
            input_schema={
                "type": "object",
                "properties": {"count": {"type": "integer"}},
                "required": ["count"],
            },
            execute=_echo,
        )
        state = _state(bus, tools=[strict])
        sandbox = Sandbox(config=None, event_bus=bus)
        tc = ToolCall(id="tc1", name="strict", arguments={"count": "not_int"})

        _result_msg, ok = await execute_tool_call(tc, state, sandbox)
        assert ok is False
        assert state.tool_calls_made == 0

    @pytest.mark.asyncio
    async def test_tool_exception(self):
        from arcrun.executor import execute_tool_call

        bus = _bus()
        bomb = Tool(
            name="bomb", description="Explodes", input_schema={"type": "object"}, execute=_explode
        )
        state = _state(bus, tools=[bomb])
        sandbox = Sandbox(config=None, event_bus=bus)
        tc = ToolCall(id="tc1", name="bomb", arguments={})

        _result_msg, ok = await execute_tool_call(tc, state, sandbox)
        assert ok is False
        assert state.tool_calls_made == 0
        error_events = [e for e in bus.events if e.type == "tool.error"]
        assert len(error_events) == 1
        assert "boom" in error_events[0].data["error"]

    @pytest.mark.asyncio
    async def test_events_emitted_on_success(self):
        from arcrun.executor import execute_tool_call

        bus = _bus()
        state = _state(bus)
        sandbox = Sandbox(config=None, event_bus=bus)
        tc = ToolCall(id="tc1", name="echo", arguments={"input": "hi"})

        await execute_tool_call(tc, state, sandbox)
        types = [e.type for e in bus.events]
        assert "tool.start" in types
        assert "tool.end" in types

    @pytest.mark.asyncio
    async def test_tool_timeout(self):
        import asyncio

        from arcrun.executor import execute_tool_call

        async def slow_tool(params, ctx):
            await asyncio.sleep(10)
            return "done"

        bus = _bus()
        tool = Tool(
            name="slow",
            description="Slow",
            input_schema={"type": "object"},
            execute=slow_tool,
            timeout_seconds=0.01,
        )
        state = _state(bus, tools=[tool])
        sandbox = Sandbox(config=None, event_bus=bus)
        tc = ToolCall(id="tc1", name="slow", arguments={})

        result_msg, ok = await execute_tool_call(tc, state, sandbox)
        assert ok is False
        assert state.tool_calls_made == 0
        assert any("timed out" in str(block) for block in result_msg.content)
        error_events = [e for e in bus.events if e.type == "tool.error"]
        assert len(error_events) == 1
        assert "timeout" in error_events[0].data["error"]

    @pytest.mark.asyncio
    async def test_tool_error_truncated_for_llm(self):
        from arcrun.executor import execute_tool_call

        async def verbose_error(params, ctx):
            raise RuntimeError("x" * 500)

        bus = _bus()
        tool = Tool(
            name="verbose",
            description="Verbose error",
            input_schema={"type": "object"},
            execute=verbose_error,
        )
        state = _state(bus, tools=[tool])
        sandbox = Sandbox(config=None, event_bus=bus)
        tc = ToolCall(id="tc1", name="verbose", arguments={})

        result_msg, ok = await execute_tool_call(tc, state, sandbox)
        assert ok is False
        # LLM message should be truncated
        llm_text = str(result_msg.content)
        assert "RuntimeError" in llm_text
        assert len(llm_text) < 500
        # Event should have full error
        error_events = [e for e in bus.events if e.type == "tool.error"]
        assert len(error_events) == 1
        assert len(error_events[0].data["error"]) == 500

    @pytest.mark.asyncio
    async def test_global_timeout_used_when_no_per_tool(self):
        import asyncio

        from arcrun.executor import execute_tool_call

        async def slow_tool(params, ctx):
            await asyncio.sleep(10)
            return "done"

        bus = _bus()
        tool = Tool(
            name="slow",
            description="Slow",
            input_schema={"type": "object"},
            execute=slow_tool,
        )
        state = _state(bus, tools=[tool])
        state.tool_timeout = 0.01
        sandbox = Sandbox(config=None, event_bus=bus)
        tc = ToolCall(id="tc1", name="slow", arguments={})

        result_msg, ok = await execute_tool_call(tc, state, sandbox)
        assert ok is False
        assert any("timed out" in str(block) for block in result_msg.content)

    @pytest.mark.asyncio
    async def test_tool_end_has_duration(self):
        from arcrun.executor import execute_tool_call

        bus = _bus()
        state = _state(bus)
        sandbox = Sandbox(config=None, event_bus=bus)
        tc = ToolCall(id="tc1", name="echo", arguments={"input": "x"})

        await execute_tool_call(tc, state, sandbox)
        end_events = [e for e in bus.events if e.type == "tool.end"]
        assert len(end_events) == 1
        assert "duration_ms" in end_events[0].data
        assert "result_length" in end_events[0].data
