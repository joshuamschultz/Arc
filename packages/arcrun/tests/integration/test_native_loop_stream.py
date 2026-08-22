"""Acceptance tests for the native provider-to-ReAct streaming path."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import arcllm
import pytest

from arcrun import (
    StaticProvider,
    TokenEvent,
    ToolEndEvent,
    ToolStartEvent,
    TurnEndEvent,
    collect,
    run,
    run_stream,
)
from arcrun.types import Tool

_USAGE = arcllm.Usage(input_tokens=0, output_tokens=0, total_tokens=0)


def _tool(counter: dict[str, int]) -> Tool:
    async def execute(arguments: dict[str, Any], _context: Any) -> str:
        counter["calls"] += 1
        return arguments["value"]

    return Tool(
        name="echo",
        description="Echo one value.",
        input_schema={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
        execute=execute,
    )


class _NativeModel:
    def __init__(self, release: asyncio.Event | None = None) -> None:
        self.release = release
        self.first_delta = asyncio.Event()
        self.stream_calls = 0
        self.invoke_calls = 0

    async def invoke(self, _messages: list[Any], tools: list[Any] | None = None, **_kwargs: Any) -> arcllm.LLMResponse:
        self.invoke_calls += 1
        if tools and any(tool.name == "select_strategy" for tool in tools):
            return arcllm.LLMResponse(
                tool_calls=[arcllm.ToolCall(id="strategy", name="select_strategy", arguments={"strategy": "react"})],
                stop_reason="tool_use",
                usage=_USAGE,
                model="test",
            )
        return arcllm.LLMResponse(content="native text", usage=_USAGE, model="test", stop_reason="end_turn")

    async def invoke_stream(self, _messages: list[Any], tools: list[Any] | None = None, **_kwargs: Any) -> AsyncIterator[arcllm.Delta]:
        self.stream_calls += 1
        if self.stream_calls == 1:
            self.first_delta.set()
            yield arcllm.Delta(text="native ")
            if self.release is not None:
                await self.release.wait()
            yield arcllm.Delta(text="text")
            yield arcllm.Delta(usage=arcllm.Usage(input_tokens=1, output_tokens=2, total_tokens=3), stop_reason="end_turn")


class _ToolStreamModel(_NativeModel):
    async def invoke_stream(self, _messages: list[Any], tools: list[Any] | None = None, **_kwargs: Any) -> AsyncIterator[arcllm.Delta]:
        self.stream_calls += 1
        if self.stream_calls == 1:
            yield arcllm.Delta(tool_call=arcllm.ToolCallDelta(index=0, id="call", name="echo"))
            yield arcllm.Delta(tool_call=arcllm.ToolCallDelta(index=0, arguments='{"value":"secret"}'))
            yield arcllm.Delta(stop_reason="tool_use")
            return
        yield arcllm.Delta(text="complete")
        yield arcllm.Delta(stop_reason="end_turn")


@pytest.mark.asyncio
async def test_native_text_arrives_before_provider_completion_and_is_monotonic() -> None:
    release = asyncio.Event()
    model = _NativeModel(release)
    counter = {"calls": 0}
    stream = await run_stream(model=model, capabilities=StaticProvider([_tool(counter)]), system_prompt="sys", task="task")
    iterator = stream.__aiter__()
    next_event = asyncio.create_task(anext(iterator))
    await asyncio.wait_for(model.first_delta.wait(), timeout=1)
    first = await asyncio.wait_for(next_event, timeout=1)
    release.set()
    events = [first, *[event async for event in iterator]]
    assert isinstance(first, TokenEvent)
    assert first.text == "native "
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert len({event.run_id for event in events}) == 1
    assert isinstance(events[-1], TurnEndEvent)
    assert sum(isinstance(event, TurnEndEvent) for event in events) == 1
    assert "reasoning" not in repr(events)


@pytest.mark.asyncio
async def test_tool_fragments_dispatch_once_without_raw_lifecycle_bodies() -> None:
    counter = {"calls": 0}
    stream = await run_stream(model=_ToolStreamModel(), capabilities=StaticProvider([_tool(counter)]), system_prompt="sys", task="task")
    events = [event async for event in stream]
    assert counter["calls"] == 1
    starts = [event for event in events if isinstance(event, ToolStartEvent)]
    ends = [event for event in events if isinstance(event, ToolEndEvent)]
    assert len(starts) == len(ends) == 1
    assert not hasattr(starts[0], "args")
    assert not hasattr(ends[0], "result")
    assert "secret" not in repr(events)
    assert isinstance(events[-1], TurnEndEvent)


@pytest.mark.asyncio
async def test_collect_stream_matches_blocking_result_without_duplicate_stream_calls() -> None:
    counter = {"calls": 0}
    streamed_model = _NativeModel()
    streamed = await collect(
        await run_stream(model=streamed_model, capabilities=StaticProvider([_tool(counter)]), system_prompt="sys", task="task")
    )
    blocking_model = _NativeModel()
    blocking = await run(blocking_model, StaticProvider([_tool(counter)]), "sys", "task")
    assert streamed.content == blocking.content == "native text"
    assert streamed_model.stream_calls == 1
    assert streamed_model.invoke_calls == 1
