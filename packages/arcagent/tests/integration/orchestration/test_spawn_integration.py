"""Integration tests for recursive agent spawning via run()."""

import pytest
from arcrun.loop import run
from arcrun.types import Tool

from ._mock_llm import LLMResponse, MockModel, ToolCall, setup_spawn_tools


async def _echo_execute(params: dict, ctx: object) -> str:
    return f"echo: {params.get('input', '')}"


async def _upper_execute(params: dict, ctx: object) -> str:
    return params.get("text", "").upper()


ECHO_TOOL = Tool(
    name="echo",
    description="Echo input",
    input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
    execute=_echo_execute,
)

UPPER_TOOL = Tool(
    name="upper",
    description="Uppercase text",
    input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    execute=_upper_execute,
)


class TestSingleSpawn:
    @pytest.mark.asyncio
    async def test_parent_spawns_child_and_gets_result(self):
        """Parent calls spawn_task, child returns result, parent continues."""
        # Parent and child share one model object — responses served in order:
        # Parent call 1: spawn_task
        # Child call 1: "Child result: 42"
        # Parent call 2: final answer
        combined_model = MockModel(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(id="tc1", name="spawn_task", arguments={"task": "compute 42"})
                    ],
                    stop_reason="tool_use",
                ),
                # This is the child's response (depth=1)
                LLMResponse(content="Child result: 42", stop_reason="end_turn"),
                # This is the parent's continuation after spawn result
                LLMResponse(content="Parent got: Child result: 42", stop_reason="end_turn"),
            ]
        )

        result = await run(
            combined_model,
            setup_spawn_tools(combined_model, [ECHO_TOOL], "You are helpful."),
            "You are helpful.",
            "Do the task.",
            depth=0,
            max_depth=3,
        )
        assert result.content == "Parent got: Child result: 42"


class TestNestedSpawn:
    @pytest.mark.asyncio
    async def test_parent_child_grandchild(self):
        """parent -> child -> grandchild (depth 0 -> 1 -> 2)."""
        # The combined model serves all three levels sequentially:
        # Parent (depth=0) call 1: spawn_task
        # Child (depth=1) call 1: spawn_task
        # Grandchild (depth=2) call 1: "Grandchild done"
        # Child (depth=1) call 2: "Child got grandchild"
        # Parent (depth=0) call 2: "Parent got child"
        combined_model = MockModel(
            [
                # Parent spawns child
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="tc1", name="spawn_task", arguments={"task": "delegate deeper"}
                        )
                    ],
                    stop_reason="tool_use",
                ),
                # Child spawns grandchild
                LLMResponse(
                    tool_calls=[
                        ToolCall(id="tc2", name="spawn_task", arguments={"task": "do leaf work"})
                    ],
                    stop_reason="tool_use",
                ),
                # Grandchild responds
                LLMResponse(content="Grandchild done.", stop_reason="end_turn"),
                # Child responds after getting grandchild result
                LLMResponse(content="Child got grandchild.", stop_reason="end_turn"),
                # Parent responds after getting child result
                LLMResponse(content="Parent got child.", stop_reason="end_turn"),
            ]
        )

        result = await run(
            combined_model,
            setup_spawn_tools(combined_model, [ECHO_TOOL], "You are helpful."),
            "You are helpful.",
            "Start task.",
            depth=0,
            max_depth=3,
        )
        assert result.content == "Parent got child."


class TestDepthLimit:
    @pytest.mark.asyncio
    async def test_spawn_returns_error_at_max_depth(self):
        """At max_depth, spawn_task is still registered but returns an error
        when invoked. Visibility no longer depth-toggled — depth check is
        enforced at execute time, matching the principle that tool registration
        is an agent decision, not an arcrun decision.
        """
        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[ToolCall(id="tc1", name="spawn_task", arguments={"task": "x"})],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="Saw max depth error.", stop_reason="end_turn"),
            ]
        )

        result = await run(
            model,
            setup_spawn_tools(model, [ECHO_TOOL], "You are helpful."),
            "You are helpful.",
            "Do task.",
            depth=3,
            max_depth=3,
        )
        assert result.content == "Saw max depth error."

        # spawn_task IS visible at every depth — agents control registration
        invoke_call = model.invoke_calls[0]
        tool_names = [t.name for t in invoke_call["tools"]]
        assert "spawn_task" in tool_names

    @pytest.mark.asyncio
    async def test_spawn_available_below_max_depth(self):
        """spawn_task should appear in tools below max_depth."""
        model = MockModel(
            [
                LLMResponse(content="Below max.", stop_reason="end_turn"),
            ]
        )

        result = await run(
            model,
            setup_spawn_tools(model, [ECHO_TOOL], "You are helpful."),
            "You are helpful.",
            "Do task.",
            depth=0,
            max_depth=3,
        )
        assert result.content == "Below max."

        invoke_call = model.invoke_calls[0]
        tool_names = [t.name for t in invoke_call["tools"]]
        assert "spawn_task" in tool_names


class TestParallelSpawn:
    @pytest.mark.asyncio
    async def test_two_parallel_spawns(self):
        """Two spawn_task calls in one turn, both execute concurrently."""
        # Parent emits two spawn_task calls in one turn
        # Then gets both results and responds
        combined_model = MockModel(
            [
                # Parent: two spawns in one turn
                LLMResponse(
                    tool_calls=[
                        ToolCall(id="tc1", name="spawn_task", arguments={"task": "task A"}),
                        ToolCall(id="tc2", name="spawn_task", arguments={"task": "task B"}),
                    ],
                    stop_reason="tool_use",
                ),
                # Child A responds
                LLMResponse(content="Result A", stop_reason="end_turn"),
                # Child B responds
                LLMResponse(content="Result B", stop_reason="end_turn"),
                # Parent finishes
                LLMResponse(content="Got both results.", stop_reason="end_turn"),
            ]
        )

        result = await run(
            combined_model,
            setup_spawn_tools(combined_model, [ECHO_TOOL], "You are helpful."),
            "You are helpful.",
            "Do parallel work.",
            depth=0,
            max_depth=3,
        )
        assert result.content == "Got both results."


class TestEventBubbling:
    @pytest.mark.asyncio
    async def test_child_events_appear_on_parent_bus(self):
        """Child events should propagate to parent bus with child prefix."""
        captured_events: list = []

        def capture_event(event):
            captured_events.append(event)

        combined_model = MockModel(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(id="tc1", name="spawn_task", arguments={"task": "child work"})
                    ],
                    stop_reason="tool_use",
                ),
                # Child
                LLMResponse(content="Child done.", stop_reason="end_turn"),
                # Parent
                LLMResponse(content="Parent done.", stop_reason="end_turn"),
            ]
        )

        result = await run(
            combined_model,
            setup_spawn_tools(combined_model, [ECHO_TOOL], "You are helpful."),
            "You are helpful.",
            "Start.",
            depth=0,
            max_depth=3,
            on_event=capture_event,
        )
        assert result.content == "Parent done."

        # Check that child events were bubbled with prefix
        event_types = [e.type for e in captured_events]
        child_events = [t for t in event_types if t.startswith("child.")]
        assert len(child_events) > 0

        # At least one should contain child_run_id in data
        child_events_with_id = [
            e for e in captured_events if e.type.startswith("child.") and "child_run_id" in e.data
        ]
        assert len(child_events_with_id) > 0


class TestChildFailure:
    @pytest.mark.asyncio
    async def test_child_exception_returns_error_string(self):
        """Child failure returns error string, parent continues."""

        class FailingChildModel:
            def __init__(self):
                self.invoke_calls = []
                self._call_count = 0

            async def invoke(self, messages, tools=None):
                self.invoke_calls.append({"messages": messages, "tools": tools})
                self._call_count += 1
                if self._call_count == 1:
                    # Parent: spawn child
                    return LLMResponse(
                        tool_calls=[
                            ToolCall(id="tc1", name="spawn_task", arguments={"task": "fail"})
                        ],
                        stop_reason="tool_use",
                    )
                elif self._call_count == 2:
                    # Child: raise exception (no tools provided)
                    raise ValueError("tools must not be empty")
                else:
                    # Parent: handle error
                    return LLMResponse(content="Handled child error.", stop_reason="end_turn")

        model = FailingChildModel()
        result = await run(
            model,
            setup_spawn_tools(model, [ECHO_TOOL], "You are helpful."),
            "You are helpful.",
            "Try spawning.",
            depth=0,
            max_depth=3,
        )
        assert result.content == "Handled child error."


class TestToolSubsetting:
    @pytest.mark.asyncio
    async def test_child_gets_only_named_tools(self):
        """Child should only get the tools specified in the tools param."""
        tool_names_seen = []

        class InspectingModel:
            def __init__(self):
                self._call_count = 0

            async def invoke(self, messages, tools=None):
                self._call_count += 1
                if tools:
                    tool_names_seen.append([t.name for t in tools])
                if self._call_count == 1:
                    # Parent: spawn with only echo
                    return LLMResponse(
                        tool_calls=[
                            ToolCall(
                                id="tc1",
                                name="spawn_task",
                                arguments={"task": "use echo only", "tools": ["echo"]},
                            )
                        ],
                        stop_reason="tool_use",
                    )
                elif self._call_count == 2:
                    # Child
                    return LLMResponse(content="Child done.", stop_reason="end_turn")
                else:
                    # Parent finishes
                    return LLMResponse(content="Parent done.", stop_reason="end_turn")

        model = InspectingModel()
        result = await run(
            model,
            setup_spawn_tools(model, [ECHO_TOOL, UPPER_TOOL], "You are helpful."),
            "You are helpful.",
            "Start.",
            depth=0,
            max_depth=3,
        )
        assert result.content == "Parent done."

        # Parent tools should include echo, upper, and spawn_task
        parent_tools = tool_names_seen[0]
        assert "echo" in parent_tools
        assert "upper" in parent_tools
        assert "spawn_task" in parent_tools

        # Child tools should include echo only — parent passed tools=["echo"]
        # so spawn is NOT inherited (parent restricts capability per spawn).
        # NOTE: arcrun no longer auto-injects spawn_task; the spawning agent
        # decides what the child can do via the explicit subset.
        child_tools = tool_names_seen[1]
        assert "echo" in child_tools
        assert "upper" not in child_tools
        assert "spawn_task" not in child_tools


class TestSystemPromptOverride:
    @pytest.mark.asyncio
    async def test_child_uses_provided_system_prompt(self):
        """Child should use the system_prompt from spawn_task args."""
        system_prompts_seen = []

        class SystemPromptTracker:
            def __init__(self):
                self._call_count = 0

            async def invoke(self, messages, tools=None):
                self._call_count += 1
                # Extract system prompt from messages
                for msg in messages:
                    if hasattr(msg, "role") and msg.role == "system":
                        system_prompts_seen.append(msg.content)
                        break

                if self._call_count == 1:
                    return LLMResponse(
                        tool_calls=[
                            ToolCall(
                                id="tc1",
                                name="spawn_task",
                                arguments={
                                    "task": "specialized work",
                                    "system_prompt": "You are a specialist.",
                                },
                            )
                        ],
                        stop_reason="tool_use",
                    )
                elif self._call_count == 2:
                    return LLMResponse(content="Specialist done.", stop_reason="end_turn")
                else:
                    return LLMResponse(content="Parent done.", stop_reason="end_turn")

        model = SystemPromptTracker()
        result = await run(
            model,
            setup_spawn_tools(model, [ECHO_TOOL], "You are a generalist."),
            "You are a generalist.",
            "Start.",
            depth=0,
            max_depth=3,
        )
        assert result.content == "Parent done."

        # Parent should have "You are a generalist."
        assert system_prompts_seen[0] == "You are a generalist."
        # Child should have parent preamble + specialization (ASI-01)
        assert "You are a generalist." in system_prompts_seen[1]
        assert "You are a specialist." in system_prompts_seen[1]
