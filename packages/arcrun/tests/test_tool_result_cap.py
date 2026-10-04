"""Tool results are capped to ``max_tool_result_tokens`` before the model sees them."""

import pytest
from packages.arcrun.tests.conftest import Message, ToolCall

from arcrun.events import EventBus
from arcrun.executor import execute_tool_call
from arcrun.registry import ToolRegistry
from arcrun.sandbox import Sandbox
from arcrun.state import RunState
from arcrun.types import Tool

CHARS_PER_TOKEN = 4


def _big_tool(size_chars: int) -> Tool:
    async def _big(params: dict, ctx: object) -> str:
        return "x" * size_chars

    return Tool(
        name="big",
        description="Returns a large result",
        input_schema={"type": "object", "properties": {}},
        execute=_big,
    )


async def _run_big(size_chars: int, **state_kwargs: object) -> tuple[str, EventBus]:
    bus = EventBus(run_id="test")
    state = RunState(
        messages=[Message(role="user", content="go")],
        registry=ToolRegistry(tools=[_big_tool(size_chars)], event_bus=bus),
        event_bus=bus,
        run_id="test-run",
        **state_kwargs,  # type: ignore[arg-type]  # reason: test passes the cap through
    )
    tc = ToolCall(id="tc1", name="big", arguments={})
    message, ok = await execute_tool_call(tc, state, Sandbox(config=None, event_bus=bus))
    assert ok is True
    return "".join(str(block) for block in message.content), bus


def _end_event(bus: EventBus):
    return next(e for e in bus.events if e.type == "tool.end")


class TestToolResultCap:
    @pytest.mark.asyncio
    async def test_under_cap_unchanged(self):
        text, bus = await _run_big(100 * CHARS_PER_TOKEN, max_tool_result_tokens=100)
        assert "truncated" not in text
        assert "x" * 400 in text
        assert "truncated" not in _end_event(bus).data

    @pytest.mark.asyncio
    async def test_over_cap_truncated_with_exact_marker(self):
        total = 1000
        cap = 100
        text, _ = await _run_big(total * CHARS_PER_TOKEN, max_tool_result_tokens=cap)
        marker = (
            f"[truncated: {total - cap} tokens omitted of {total} total. "
            "Call big again with a narrower request "
            "(e.g. offset/limit or a more specific query) to read more.]"
        )
        assert text.count("x") == cap * CHARS_PER_TOKEN
        assert marker in text
        assert text.index("x") < text.index(marker)

    @pytest.mark.asyncio
    async def test_none_disables_cap(self):
        text, bus = await _run_big(50_000 * CHARS_PER_TOKEN, max_tool_result_tokens=None)
        assert "truncated" not in text
        assert text.count("x") == 50_000 * CHARS_PER_TOKEN
        assert "truncated" not in _end_event(bus).data

    @pytest.mark.asyncio
    async def test_default_cap_is_8000_tokens(self):
        text, _ = await _run_big(9000 * CHARS_PER_TOKEN)
        assert text.count("x") == 8000 * CHARS_PER_TOKEN
        assert "[truncated: 1000 tokens omitted of 9000 total." in text

    @pytest.mark.asyncio
    async def test_event_carries_truncated_flag_and_original_size(self):
        _, bus = await _run_big(1000 * CHARS_PER_TOKEN, max_tool_result_tokens=100)
        data = _end_event(bus).data
        assert data["truncated"] is True
        assert data["original_tokens"] == 1000
        assert data["original_length"] == 1000 * CHARS_PER_TOKEN
