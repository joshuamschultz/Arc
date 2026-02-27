"""Integration tests for CodeExec: strategy selection + ExecuteTool + CodeExecStrategy."""

import pytest
from conftest import LLMResponse, MockModel, ToolCall

from arcrun.builtins import make_execute_tool
from arcrun.types import SandboxConfig, Tool


async def _echo(params: dict, ctx: object) -> str:
    return f"echo: {params.get('input', '')}"


def _tools_with_execute() -> list[Tool]:
    return [
        Tool(
            name="echo",
            description="Echo input",
            input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
            execute=_echo,
        ),
        make_execute_tool(timeout_seconds=5),
    ]


class TestCodeExecIntegration:
    @pytest.mark.asyncio
    async def test_run_with_allowed_strategies_selects(self):
        from arcrun.loop import run

        model = MockModel(
            [
                # Selection call: model picks "code"
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="sel1",
                            name="select_strategy",
                            arguments={"strategy": "code"},
                        )
                    ],
                    stop_reason="tool_use",
                ),
                # Strategy execution: model responds
                LLMResponse(content="Done with code.", stop_reason="end_turn"),
            ]
        )
        result = await run(
            model,
            _tools_with_execute(),
            "Be helpful.",
            "Compute 2+2",
            allowed_strategies=["react", "code"],
        )
        assert result.strategy_used == "code"
        assert result.content == "Done with code."

    @pytest.mark.asyncio
    async def test_code_exec_end_to_end(self):
        from arcrun.loop import run

        model = MockModel(
            [
                # Model writes code
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="tc1",
                            name="execute_python",
                            arguments={"code": "print(2 + 2)"},
                        )
                    ],
                    stop_reason="tool_use",
                ),
                # Model responds with result
                LLMResponse(content="The answer is 4.", stop_reason="end_turn"),
            ]
        )
        result = await run(
            model,
            _tools_with_execute(),
            "Be helpful.",
            "What is 2+2?",
            allowed_strategies=["code"],
        )
        assert result.content == "The answer is 4."
        assert result.tool_calls_made == 1

        # Verify the tool result contained structured JSON
        tool_end_events = [e for e in result.events if e.type == "tool.end"]
        assert len(tool_end_events) == 1
        assert tool_end_events[0].data["name"] == "execute_python"

    @pytest.mark.asyncio
    async def test_sandbox_denies_execute_python(self):
        from arcrun.loop import run

        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="tc1",
                            name="execute_python",
                            arguments={"code": "print('hack')"},
                        )
                    ],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="Denied.", stop_reason="end_turn"),
            ]
        )
        cfg = SandboxConfig(allowed_tools=["echo"])
        result = await run(
            model,
            _tools_with_execute(),
            "Be helpful.",
            "Run code",
            allowed_strategies=["code"],
            sandbox=cfg,
        )
        denied = [e for e in result.events if e.type == "tool.denied"]
        assert len(denied) == 1
        assert denied[0].data["name"] == "execute_python"

    @pytest.mark.asyncio
    async def test_event_completeness(self):
        from arcrun.loop import run

        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="sel1",
                            name="select_strategy",
                            arguments={"strategy": "code"},
                        )
                    ],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="Done.", stop_reason="end_turn"),
            ]
        )
        result = await run(
            model,
            _tools_with_execute(),
            "Be helpful.",
            "Task",
            allowed_strategies=["react", "code"],
        )
        event_types = {e.type for e in result.events}
        assert "strategy.selection.start" in event_types
        assert "strategy.selection.complete" in event_types
        assert "strategy.selected" in event_types
        assert "code.prompt.augmented" in event_types
        assert "loop.start" in event_types
        assert "loop.complete" in event_types

    @pytest.mark.asyncio
    async def test_existing_react_still_works(self):
        from arcrun.loop import run

        model = MockModel([LLMResponse(content="Hello!", stop_reason="end_turn")])
        result = await run(
            model,
            _tools_with_execute(),
            "Be helpful.",
            "Say hello",
        )
        assert result.content == "Hello!"
        assert result.strategy_used == "react"

    @pytest.mark.asyncio
    async def test_public_exports(self):
        from arcrun import Strategy, make_execute_tool

        assert Strategy is not None
        assert callable(make_execute_tool)
