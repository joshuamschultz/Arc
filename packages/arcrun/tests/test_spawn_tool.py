"""Unit tests for make_spawn_tool() factory."""

import asyncio

import pytest
from conftest import LLMResponse, Message, MockModel

from arcrun.builtins.spawn import _make_bubble_handler, make_spawn_tool
from arcrun.events import EventBus
from arcrun.registry import ToolRegistry
from arcrun.state import RunState
from arcrun.types import Tool, ToolContext


async def _echo_execute(params: dict, ctx: object) -> str:
    return f"echo: {params.get('input', '')}"


async def _upper_execute(params: dict, ctx: object) -> str:
    return params.get("text", "").upper()


def _make_parent_state(*, depth: int = 0, max_depth: int = 3) -> RunState:
    bus = EventBus(run_id="parent-run")
    tools = [
        Tool(
            name="echo",
            description="Echo input",
            input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
            execute=_echo_execute,
        ),
        Tool(
            name="upper",
            description="Uppercase text",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
            execute=_upper_execute,
        ),
    ]
    reg = ToolRegistry(tools=tools, event_bus=bus)
    return RunState(
        messages=[
            Message(role="system", content="You are helpful."),
            Message(role="user", content="Do the task."),
        ],
        registry=reg,
        event_bus=bus,
        run_id="parent-run",
        depth=depth,
        max_depth=max_depth,
    )


def _make_ctx() -> ToolContext:
    return ToolContext(
        run_id="parent-run",
        tool_call_id="tc1",
        turn_number=1,
        event_bus=EventBus(run_id="parent-run"),
        cancelled=asyncio.Event(),
    )


class TestMakeSpawnTool:
    def test_factory_returns_tool(self):
        state = _make_parent_state()
        model = MockModel([])
        tools = [
            Tool(
                name="echo",
                description="Echo",
                input_schema={"type": "object"},
                execute=_echo_execute,
            ),
        ]
        tool = make_spawn_tool(
            model=model,
            tools=tools,
            system_prompt="test",
            state=state,
        )
        assert isinstance(tool, Tool)

    def test_tool_name_is_spawn_task(self):
        state = _make_parent_state()
        model = MockModel([])
        tools = [
            Tool(
                name="echo",
                description="Echo",
                input_schema={"type": "object"},
                execute=_echo_execute,
            ),
        ]
        tool = make_spawn_tool(
            model=model,
            tools=tools,
            system_prompt="test",
            state=state,
        )
        assert tool.name == "spawn_task"

    def test_schema_has_task_required(self):
        state = _make_parent_state()
        model = MockModel([])
        tool = make_spawn_tool(
            model=model,
            tools=[],
            system_prompt="test",
            state=state,
        )
        assert "task" in tool.input_schema["properties"]
        assert "task" in tool.input_schema["required"]

    def test_schema_has_optional_system_prompt(self):
        state = _make_parent_state()
        model = MockModel([])
        tool = make_spawn_tool(
            model=model,
            tools=[],
            system_prompt="test",
            state=state,
        )
        assert "system_prompt" in tool.input_schema["properties"]
        assert "system_prompt" not in tool.input_schema["required"]

    def test_schema_has_optional_tools(self):
        state = _make_parent_state()
        model = MockModel([])
        tool = make_spawn_tool(
            model=model,
            tools=[],
            system_prompt="test",
            state=state,
        )
        assert "tools" in tool.input_schema["properties"]
        assert "tools" not in tool.input_schema["required"]

    def test_tool_timeout_is_none(self):
        state = _make_parent_state()
        model = MockModel([])
        tool = make_spawn_tool(
            model=model,
            tools=[],
            system_prompt="test",
            state=state,
        )
        assert tool.timeout_seconds == 300  # default spawn timeout (C4 fix)


class TestDepthLimitRejection:
    @pytest.mark.asyncio
    async def test_depth_at_max_returns_error(self):
        state = _make_parent_state(depth=3, max_depth=3)
        model = MockModel([])
        tools = [
            Tool(
                name="echo",
                description="Echo",
                input_schema={"type": "object"},
                execute=_echo_execute,
            ),
        ]
        tool = make_spawn_tool(
            model=model,
            tools=tools,
            system_prompt="test",
            state=state,
        )
        ctx = _make_ctx()
        result = await tool.execute({"task": "do something"}, ctx)
        assert "Error" in result
        assert "max spawn depth" in result

    @pytest.mark.asyncio
    async def test_depth_exceeds_max_returns_error(self):
        state = _make_parent_state(depth=5, max_depth=3)
        model = MockModel([])
        tool = make_spawn_tool(
            model=model,
            tools=[],
            system_prompt="test",
            state=state,
        )
        ctx = _make_ctx()
        result = await tool.execute({"task": "do something"}, ctx)
        assert "Error" in result


class TestToolSubsetting:
    @pytest.mark.asyncio
    async def test_unknown_tool_name_returns_error(self):
        state = _make_parent_state(depth=0, max_depth=3)
        model = MockModel([LLMResponse(content="Done.", stop_reason="end_turn")])
        tools = [
            Tool(
                name="echo",
                description="Echo",
                input_schema={"type": "object"},
                execute=_echo_execute,
            ),
        ]
        tool = make_spawn_tool(
            model=model,
            tools=tools,
            system_prompt="test",
            state=state,
        )
        ctx = _make_ctx()
        result = await tool.execute({"task": "do X", "tools": ["nonexistent"]}, ctx)
        assert "Error" in result
        assert "unknown tool" in result
        assert "nonexistent" in result

    @pytest.mark.asyncio
    async def test_valid_tool_subset_succeeds(self):
        state = _make_parent_state(depth=0, max_depth=3)
        child_model = MockModel([LLMResponse(content="Child done.", stop_reason="end_turn")])
        tools = [
            Tool(
                name="echo",
                description="Echo",
                input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
                execute=_echo_execute,
            ),
            Tool(
                name="upper",
                description="Upper",
                input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
                execute=_upper_execute,
            ),
        ]
        tool = make_spawn_tool(
            model=child_model,
            tools=tools,
            system_prompt="test",
            state=state,
        )
        ctx = _make_ctx()
        result = await tool.execute({"task": "do X", "tools": ["echo"]}, ctx)
        assert result == "Child done."


class TestBubbleHandler:
    def test_bubble_handler_emits_prefixed_event(self):
        parent_bus = EventBus(run_id="parent-run")
        handler = _make_bubble_handler("child-123", parent_bus)

        # Simulate a child event
        from arcrun.events import Event

        child_event = Event(
            type="tool.start",
            timestamp=1.0,
            run_id="child-123",
            data={"name": "echo"},
        )
        handler(child_event)

        # Check parent bus received prefixed event
        assert len(parent_bus.events) == 1
        event = parent_bus.events[0]
        assert event.type == "child.child-123.tool.start"
        assert event.data["child_run_id"] == "child-123"
        assert event.data["name"] == "echo"

    def test_bubble_handler_preserves_child_data(self):
        parent_bus = EventBus(run_id="parent-run")
        handler = _make_bubble_handler("child-456", parent_bus)

        from arcrun.events import Event

        child_event = Event(
            type="loop.complete",
            timestamp=2.0,
            run_id="child-456",
            data={"content": "result", "turns": 2},
        )
        handler(child_event)

        event = parent_bus.events[0]
        assert event.data["content"] == "result"
        assert event.data["turns"] == 2
        assert event.data["child_run_id"] == "child-456"
