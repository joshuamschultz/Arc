"""Tests for collect() — drain a StreamEvent iterator to a final RunResult.

collect() is the one-shot helper for callers that drive the agent through
the streaming entry but only want the final answer (CLI, scheduler, module
callbacks). It consumes the whole stream and reconstructs the result from
the terminal TurnEndEvent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from arcrun import RunResult, StreamEvent, TokenEvent, TurnEndEvent, collect


async def _stream(*events: StreamEvent) -> AsyncIterator[StreamEvent]:
    for ev in events:
        yield ev


@pytest.mark.asyncio
async def test_collect_returns_final_result() -> None:
    """collect() drains the stream and returns the TurnEndEvent's totals."""
    stream = _stream(
        TokenEvent(text="hello"),
        TokenEvent(text=" world"),
        TurnEndEvent(final_text="hello world", turns=2, tool_calls_made=1, cost_usd=0.5),
    )

    result = await collect(stream)

    assert isinstance(result, RunResult)
    assert result.content == "hello world"
    assert result.turns == 2
    assert result.tool_calls_made == 1
    assert result.cost_usd == 0.5


@pytest.mark.asyncio
async def test_collect_falls_back_to_token_text_without_turn_end() -> None:
    """A stream with no TurnEndEvent still yields the concatenated tokens."""
    stream = _stream(TokenEvent(text="par"), TokenEvent(text="tial"))

    result = await collect(stream)

    assert result.content == "partial"
    assert result.turns == 0
