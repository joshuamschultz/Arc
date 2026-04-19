"""Integration tests for arcrun.run_stream() per-token streaming.

Verifies that run_stream() yields tokens incrementally, emits tool events,
and always terminates with a TurnEndEvent.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcrun import (
    StreamEvent,
    TokenEvent,
    ToolEndEvent,
    ToolStartEvent,
    TurnEndEvent,
    run_stream,
)
from arcrun.types import LoopResult, Tool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_tool() -> Tool:
    """A trivial tool for tests that do not need real tool execution."""

    async def _execute(args: dict[str, Any], ctx: Any) -> str:
        return "tool-result"

    return Tool(
        name="echo",
        description="Echo args back.",
        input_schema={"type": "object", "properties": {}, "required": []},
        execute=_execute,
    )


def _fake_model(response_text: str = "hello from model") -> Any:
    """Return a mock model that returns response_text without tool calls."""
    model = MagicMock()
    loop_result = LoopResult(
        content=response_text,
        turns=1,
        tool_calls_made=0,
        tokens_used={},
        strategy_used="react",
        cost_usd=0.0,
    )
    return model, loop_result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_stream_yields_token_and_turn_end() -> None:
    """run_stream() must yield at least a TokenEvent and TurnEndEvent."""
    response_text = "streaming response text"

    # Patch the strategy to return a LoopResult with our text.
    loop_result = LoopResult(
        content=response_text,
        turns=1,
        tool_calls_made=0,
        tokens_used={},
        strategy_used="react",
        cost_usd=0.0,
    )

    async def _fake_strategy(model: Any, state: Any, sandbox: Any, max_turns: int) -> LoopResult:
        return loop_result

    with (
        patch("arcrun.loop.select_strategy", new=AsyncMock(return_value="react")),
        patch.dict("arcrun.loop.STRATEGIES", {"react": _fake_strategy}),
    ):
        stream = await run_stream(
            model=MagicMock(),
            tools=[_fake_tool()],
            system_prompt="You are a helpful assistant.",
            task="Say hello",
        )

        events: list[StreamEvent] = []
        async for event in stream:
            events.append(event)

    assert len(events) >= 2, "Expected at least TokenEvent + TurnEndEvent"

    token_events = [e for e in events if isinstance(e, TokenEvent)]
    turn_end_events = [e for e in events if isinstance(e, TurnEndEvent)]

    assert len(token_events) >= 1, "Expected at least one TokenEvent"
    assert len(turn_end_events) == 1, "Expected exactly one TurnEndEvent"

    # The TurnEndEvent final_text must match the loop result content.
    assert turn_end_events[0].final_text == response_text

    # TurnEndEvent must be last.
    assert isinstance(events[-1], TurnEndEvent)


@pytest.mark.asyncio
async def test_run_stream_token_text_matches_result() -> None:
    """TokenEvent.text combined must equal the LoopResult.content."""
    response_text = "the quick brown fox"

    loop_result = LoopResult(
        content=response_text,
        turns=1,
        tool_calls_made=0,
        tokens_used={},
        strategy_used="react",
        cost_usd=0.0,
    )

    async def _fake_strategy(model: Any, state: Any, sandbox: Any, max_turns: int) -> LoopResult:
        return loop_result

    with (
        patch("arcrun.loop.select_strategy", new=AsyncMock(return_value="react")),
        patch.dict("arcrun.loop.STRATEGIES", {"react": _fake_strategy}),
    ):
        stream = await run_stream(
            model=MagicMock(),
            tools=[_fake_tool()],
            system_prompt="sys",
            task="task",
        )

        token_texts: list[str] = []
        async for event in stream:
            if isinstance(event, TokenEvent):
                token_texts.append(event.text)

    combined = "".join(token_texts)
    assert combined == response_text


@pytest.mark.asyncio
async def test_run_stream_tool_events_emitted() -> None:
    """run_stream() yields ToolStartEvent and ToolEndEvent when tools are called.

    Verifies the on_event bridge correctly maps arcrun EventBus events to
    StreamEvent subclasses.
    """
    loop_result = LoopResult(
        content="done",
        turns=2,
        tool_calls_made=1,
        tokens_used={},
        strategy_used="react",
        cost_usd=0.0,
    )

    async def _strategy_with_tool_events(
        model: Any, state: Any, sandbox: Any, max_turns: int
    ) -> LoopResult:
        # Emit tool events directly on the EventBus to simulate a tool call.
        state.event_bus.emit("tool.start", {"name": "echo", "args": {}})
        state.event_bus.emit("tool.end", {"result": "tool-result"})
        return loop_result

    with (
        patch("arcrun.loop.select_strategy", new=AsyncMock(return_value="react")),
        patch.dict("arcrun.loop.STRATEGIES", {"react": _strategy_with_tool_events}),
    ):
        stream = await run_stream(
            model=MagicMock(),
            tools=[_fake_tool()],
            system_prompt="sys",
            task="use the tool",
        )

        events: list[StreamEvent] = []
        async for event in stream:
            events.append(event)

    tool_starts = [e for e in events if isinstance(e, ToolStartEvent)]
    tool_ends = [e for e in events if isinstance(e, ToolEndEvent)]

    assert len(tool_starts) >= 1, "Expected at least one ToolStartEvent"
    assert tool_starts[0].name == "echo"
    assert len(tool_ends) >= 1, "Expected at least one ToolEndEvent"


@pytest.mark.asyncio
async def test_run_stream_turn_end_is_last() -> None:
    """TurnEndEvent must always be the final event in the stream."""
    loop_result = LoopResult(
        content="final",
        turns=1,
        tool_calls_made=0,
        tokens_used={},
        strategy_used="react",
        cost_usd=0.0,
    )

    async def _fake_strategy(model: Any, state: Any, sandbox: Any, max_turns: int) -> LoopResult:
        state.event_bus.emit("tool.start", {"name": "echo", "args": {}})
        state.event_bus.emit("tool.end", {"result": "r"})
        return loop_result

    with (
        patch("arcrun.loop.select_strategy", new=AsyncMock(return_value="react")),
        patch.dict("arcrun.loop.STRATEGIES", {"react": _fake_strategy}),
    ):
        stream = await run_stream(
            model=MagicMock(),
            tools=[_fake_tool()],
            system_prompt="sys",
            task="task",
        )

        events: list[StreamEvent] = []
        async for event in stream:
            events.append(event)

    assert isinstance(events[-1], TurnEndEvent), (
        "Last event must always be TurnEndEvent; got "
        f"{type(events[-1]).__name__}"
    )


@pytest.mark.asyncio
async def test_run_stream_empty_content() -> None:
    """run_stream() handles None / empty content gracefully."""
    loop_result = LoopResult(
        content=None,
        turns=1,
        tool_calls_made=0,
        tokens_used={},
        strategy_used="react",
        cost_usd=0.0,
    )

    async def _fake_strategy(model: Any, state: Any, sandbox: Any, max_turns: int) -> LoopResult:
        return loop_result

    with (
        patch("arcrun.loop.select_strategy", new=AsyncMock(return_value="react")),
        patch.dict("arcrun.loop.STRATEGIES", {"react": _fake_strategy}),
    ):
        stream = await run_stream(
            model=MagicMock(),
            tools=[_fake_tool()],
            system_prompt="sys",
            task="task",
        )

        events: list[StreamEvent] = []
        async for event in stream:
            events.append(event)

    turn_end_events = [e for e in events if isinstance(e, TurnEndEvent)]
    assert len(turn_end_events) == 1
    assert turn_end_events[0].final_text == ""
