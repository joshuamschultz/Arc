"""Unit tests for make_spawn_tool() factory."""

import asyncio

import pytest
from arcrun.events import EventBus
from arcrun.registry import ToolRegistry
from arcrun.state import RunState
from arcrun.types import Tool, ToolContext

from arcagent.orchestration.spawn import (
    RootTokenBudget,
    _make_bubble_handler,
    make_spawn_tool,
)

from ._mock_llm import LLMResponse, Message, MockModel, ToolCall


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


def _make_ctx(parent_state: RunState | None = None) -> ToolContext:
    return ToolContext(
        run_id="parent-run",
        tool_call_id="tc1",
        turn_number=1,
        event_bus=EventBus(run_id="parent-run"),
        cancelled=asyncio.Event(),
        parent_state=parent_state,
    )


class TestMakeSpawnTool:
    def test_factory_returns_tool(self):
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
        )
        assert isinstance(tool, Tool)

    def test_tool_name_is_spawn_task(self):
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
        )
        assert tool.name == "spawn_task"

    def test_schema_has_task_required(self):
        model = MockModel([])
        tool = make_spawn_tool(
            model=model,
            tools=[],
            system_prompt="test",
        )
        assert "task" in tool.input_schema["properties"]
        assert "task" in tool.input_schema["required"]

    def test_schema_has_optional_system_prompt(self):
        model = MockModel([])
        tool = make_spawn_tool(
            model=model,
            tools=[],
            system_prompt="test",
        )
        assert "system_prompt" in tool.input_schema["properties"]
        assert "system_prompt" not in tool.input_schema["required"]

    def test_schema_has_optional_tools(self):
        model = MockModel([])
        tool = make_spawn_tool(
            model=model,
            tools=[],
            system_prompt="test",
        )
        assert "tools" in tool.input_schema["properties"]
        assert "tools" not in tool.input_schema["required"]

    def test_tool_timeout_is_none(self):
        model = MockModel([])
        tool = make_spawn_tool(
            model=model,
            tools=[],
            system_prompt="test",
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
        )
        ctx = _make_ctx(parent_state=state)
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
        )
        ctx = _make_ctx(parent_state=state)
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
        )
        ctx = _make_ctx(parent_state=state)
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
        )
        ctx = _make_ctx(parent_state=state)
        result = await tool.execute({"task": "do X", "tools": ["echo"]}, ctx)
        assert result == "Child done."


def _echo_tool() -> Tool:
    return Tool(
        name="echo",
        description="Echo input",
        input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
        execute=_echo_execute,
    )


class TestRootTokenBudgetWiring:
    """The LLM10 cross-child token pool is enforced on the live spawn path."""

    @pytest.mark.asyncio
    async def test_pool_exhaustion_refuses_further_spawns(self):
        # Pool holds exactly one child's worth of tokens (default Usage=15/call).
        pool = RootTokenBudget(total=15)
        state = _make_parent_state(depth=0, max_depth=3)
        child_model = MockModel([LLMResponse(content="first child", stop_reason="end_turn")])
        tool = make_spawn_tool(
            model=child_model,
            tools=[_echo_tool()],
            system_prompt="test",
            root_token_budget=pool,
        )
        ctx = _make_ctx(parent_state=state)

        first = await tool.execute({"task": "do A"}, ctx)
        assert first == "first child"
        # The child's actual usage was debited into the shared pool.
        assert pool.is_exhausted()

        # A second spawn is refused without ever invoking the model again.
        second = await tool.execute({"task": "do B"}, ctx)
        assert "Error" in second
        assert "budget exhausted" in second
        assert len(child_model.invoke_calls) == 1

    @pytest.mark.asyncio
    async def test_child_is_stopped_when_it_would_overrun_the_pool(self):
        # A multi-turn child clamped to the pool's remaining balance is halted at
        # the turn boundary once its cumulative tokens reach the cap.
        pool = RootTokenBudget(total=15)
        state = _make_parent_state(depth=0, max_depth=3)
        child_model = MockModel(
            [
                LLMResponse(
                    tool_calls=[ToolCall(id="t1", name="echo", arguments={"input": "x"})],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="second turn", stop_reason="end_turn"),
            ]
        )
        tool = make_spawn_tool(
            model=child_model,
            tools=[_echo_tool()],
            system_prompt="test",
            root_token_budget=pool,
        )
        ctx = _make_ctx(parent_state=state)

        await tool.execute({"task": "loop"}, ctx)
        # The clamp (max_tokens == pool remaining) stops the child before its
        # second model call — without it the child would have invoked twice.
        assert len(child_model.invoke_calls) == 1

    @pytest.mark.asyncio
    async def test_no_budget_leaves_child_unclamped(self):
        # Control: same child, no pool — both turns run, proving the clamp above
        # is what stopped it, not the mock running out of responses.
        state = _make_parent_state(depth=0, max_depth=3)
        child_model = MockModel(
            [
                LLMResponse(
                    tool_calls=[ToolCall(id="t1", name="echo", arguments={"input": "x"})],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="second turn", stop_reason="end_turn"),
            ]
        )
        tool = make_spawn_tool(
            model=child_model,
            tools=[_echo_tool()],
            system_prompt="test",
        )
        ctx = _make_ctx(parent_state=state)

        result = await tool.execute({"task": "loop"}, ctx)
        assert result == "second turn"
        assert len(child_model.invoke_calls) == 2


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
