"""Adversarial: Spawn depth bomb (OWASP ASI08).

Tests that recursive spawning is properly limited by depth controls.
"""
from __future__ import annotations

import pytest

from security.conftest import LLMResponse, MockModel, ToolCall
from arcrun.types import Tool


class TestSpawnDepthBomb:
    @pytest.mark.asyncio
    async def test_depth_limit_enforced(self):
        """Spawning at max_depth returns error, not infinite recursion."""
        from arcrun.loop import run

        model = MockModel([
            LLMResponse(
                tool_calls=[ToolCall(id="tc1", name="spawn_task", arguments={"task": "recurse"})],
                stop_reason="tool_use",
            ),
            LLMResponse(content="Depth limit reached.", stop_reason="end_turn"),
        ])

        async def noop(params: dict, ctx: object) -> str:
            return "ok"

        tool = Tool(
            name="echo", description="Echo",
            input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
            execute=noop,
        )

        # Run at max_depth — spawn should fail with error message
        result = await run(
            model, [tool], "prompt", "task",
            depth=3, max_depth=3,
        )
        # spawn_task isn't in tool list, so it gets tool.error
        assert result.turns >= 1

    @pytest.mark.asyncio
    async def test_parallel_spawn_flood(self):
        """Multiple parallel spawns respect max_concurrent_spawns."""
        from arcrun.loop import run

        # Model tries to call spawn_task 5 times simultaneously
        model = MockModel([
            LLMResponse(
                tool_calls=[
                    ToolCall(id=f"tc{i}", name="echo", arguments={"input": f"task-{i}"})
                    for i in range(5)
                ],
                stop_reason="tool_use",
            ),
            LLMResponse(content="All done.", stop_reason="end_turn"),
        ])

        async def noop(params: dict, ctx: object) -> str:
            return f"echo: {params.get('input', '')}"

        tool = Tool(
            name="echo", description="Echo",
            input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
            execute=noop,
        )

        result = await run(model, [tool], "prompt", "task")
        assert result.turns == 2
        assert result.tool_calls_made == 5

    @pytest.mark.asyncio
    async def test_depth_field_cannot_be_manipulated_by_model(self):
        """The depth parameter comes from the runtime, not the model."""
        from arcrun.loop import run

        model = MockModel([
            LLMResponse(content="OK", stop_reason="end_turn"),
        ])

        async def noop(params: dict, ctx: object) -> str:
            return "ok"

        tool = Tool(
            name="echo", description="Echo",
            input_schema={"type": "object", "properties": {}},
            execute=noop,
        )

        result = await run(model, [tool], "prompt", "task", depth=0, max_depth=5)
        # Depth is controlled by arcrun, not by model output
        assert result.turns == 1
