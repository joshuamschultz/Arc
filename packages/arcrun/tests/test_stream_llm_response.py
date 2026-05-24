"""Tests for ``stream_llm_response`` — single-call streaming primitive."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from arcrun import TokenEvent, TurnEndEvent, stream_llm_response


class _RecordingModel:
    """Yields a fixed sequence of Delta-shaped dicts.

    Defined inline (not arcllm.types.Delta) so this test stays inside
    the arcrun package — duck-typing means ``stream_llm_response`` only
    needs ``.text``, ``.usage``, ``.stop_reason`` attributes.
    """

    class _Delta:
        def __init__(self, text=None, usage=None, stop_reason=None):
            self.text = text
            self.usage = usage
            self.stop_reason = stop_reason
            self.tool_call = None

    def __init__(self, chunks: list[str], stop_reason: str = "end_turn") -> None:
        self._chunks = chunks
        self._stop_reason = stop_reason
        self.invoke_args: dict[str, Any] = {}

    async def invoke_stream(
        self,
        messages: list[Any],
        tools: list[Any] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        self.invoke_args = {
            "messages": messages,
            "tools": tools,
            "kwargs": kwargs,
        }
        for chunk in self._chunks:
            yield self._Delta(text=chunk)
        yield self._Delta(stop_reason=self._stop_reason)


@pytest.mark.asyncio
async def test_yields_token_events_then_turn_end() -> None:
    model = _RecordingModel(["Hello", ", ", "world!"])
    events: list[Any] = []
    async for ev in stream_llm_response(model=model, messages=[]):
        events.append(ev)

    assert isinstance(events[-1], TurnEndEvent)
    token_events = [e for e in events[:-1] if isinstance(e, TokenEvent)]
    assert [t.text for t in token_events] == ["Hello", ", ", "world!"]
    assert events[-1].final_text == "Hello, world!"
    assert events[-1].turns == 1


@pytest.mark.asyncio
async def test_forwards_messages_and_kwargs_to_model() -> None:
    model = _RecordingModel(["x"])
    async for _ in stream_llm_response(
        model=model,
        messages=[{"role": "user", "content": "hi"}],
        response_format={"type": "text"},
    ):
        pass
    assert model.invoke_args["messages"] == [{"role": "user", "content": "hi"}]
    assert "response_format" in model.invoke_args["kwargs"]


@pytest.mark.asyncio
async def test_empty_response_still_yields_turn_end() -> None:
    """A model that streams no text still produces a clean TurnEndEvent."""
    model = _RecordingModel([])
    events: list[Any] = []
    async for ev in stream_llm_response(model=model, messages=[]):
        events.append(ev)

    assert len(events) == 1
    assert isinstance(events[0], TurnEndEvent)
    assert events[0].final_text == ""
