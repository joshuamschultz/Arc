"""Integration tests — full end-to-end scenarios."""

import pytest
from conftest import LLMResponse, MockModel, ToolCall

from arcrun.types import SandboxConfig, Tool


async def _search(params: dict, ctx: object) -> str:
    return f"Found results for: {params['query']}"


async def _calculate(params: dict, ctx: object) -> str:
    return f"Result of: {params['expression']}"


def _tools() -> list[Tool]:
    return [
        Tool(
            name="search",
            description="Search the web",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            execute=_search,
        ),
        Tool(
            name="calculate",
            description="Calculate expression",
            input_schema={
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
            },
            execute=_calculate,
        ),
    ]


class TestIntegration:
    @pytest.mark.asyncio
    async def test_full_multi_tool_scenario(self):
        """Model reasons, calls tools, gets results, makes more calls, finishes."""
        from arcrun.loop import run

        model = MockModel(
            [
                # Turn 1: model searches
                LLMResponse(
                    content="Let me search for that.",
                    tool_calls=[
                        ToolCall(id="tc1", name="search", arguments={"query": "python async"})
                    ],
                    stop_reason="tool_use",
                ),
                # Turn 2: model calculates based on search
                LLMResponse(
                    content="Now let me calculate.",
                    tool_calls=[
                        ToolCall(id="tc2", name="calculate", arguments={"expression": "2+2"})
                    ],
                    stop_reason="tool_use",
                ),
                # Turn 3: model finishes
                LLMResponse(content="The answer is 4.", stop_reason="end_turn"),
            ]
        )

        result = await run(model, _tools(), "Be helpful.", "Find and calculate")
        assert result.content == "The answer is 4."
        assert result.turns == 3
        assert result.tool_calls_made == 2

    @pytest.mark.asyncio
    async def test_dynamic_tool_denied_by_sandbox(self):
        """Add tool mid-execution, verify denied by sandbox."""
        from arcrun.loop import run

        events = []
        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[ToolCall(id="tc1", name="search", arguments={"query": "test"})],
                    stop_reason="tool_use",
                ),
                # Model tries dynamic tool
                LLMResponse(
                    tool_calls=[ToolCall(id="tc2", name="dynamic", arguments={})],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="OK.", stop_reason="end_turn"),
            ]
        )

        cfg = SandboxConfig(allowed_tools=["search", "calculate"])
        await run(
            model,
            _tools(),
            "prompt",
            "task",
            sandbox=cfg,
            on_event=lambda e: events.append(e),
        )
        denied = [e for e in events if e.type == "tool.denied"]
        assert len(denied) == 1
        assert denied[0].data["name"] == "dynamic"

    @pytest.mark.asyncio
    async def test_event_log_completeness(self):
        """Every action has corresponding event."""
        from arcrun.loop import run

        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[ToolCall(id="tc1", name="search", arguments={"query": "x"})],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="Done.", stop_reason="end_turn"),
            ]
        )

        result = await run(model, _tools(), "prompt", "task")
        types = [e.type for e in result.events]

        # Must have these event types
        assert "loop.start" in types
        assert "turn.start" in types
        assert "llm.call" in types
        assert "tool.start" in types
        assert "tool.end" in types
        assert "turn.end" in types
        assert "loop.complete" in types

        # Correct ordering
        assert types.index("loop.start") < types.index("turn.start")
        assert types.index("tool.start") < types.index("tool.end")
        assert types.index("turn.end") < types.index("loop.complete")

    @pytest.mark.asyncio
    async def test_cost_token_accumulation(self):
        """LoopResult totals match individual events."""
        from arcrun.loop import run

        model = MockModel(
            [
                LLMResponse(
                    tool_calls=[ToolCall(id="tc1", name="search", arguments={"query": "x"})],
                    stop_reason="tool_use",
                    cost_usd=0.001,
                ),
                LLMResponse(content="Done.", stop_reason="end_turn", cost_usd=0.002),
            ]
        )

        result = await run(model, _tools(), "prompt", "task")
        assert result.cost_usd == pytest.approx(0.003, abs=1e-9)
        assert result.tokens_used["total"] == 30  # 15 per call x 2 calls
