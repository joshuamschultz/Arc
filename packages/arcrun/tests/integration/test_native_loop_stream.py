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
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        execute=execute,
    )


class _NativeModel:
    def __init__(self, release: asyncio.Event | None = None) -> None:
        self.release = release
        self.first_delta = asyncio.Event()
        self.stream_calls = 0
        self.invoke_calls = 0

    async def invoke(
        self, _messages: list[Any], tools: list[Any] | None = None, **_kwargs: Any
    ) -> arcllm.LLMResponse:
        self.invoke_calls += 1
        if tools and any(tool.name == "select_strategy" for tool in tools):
            return arcllm.LLMResponse(
                tool_calls=[
                    arcllm.ToolCall(
                        id="strategy", name="select_strategy", arguments={"strategy": "react"}
                    )
                ],
                stop_reason="tool_use",
                usage=_USAGE,
                model="test",
            )
        return arcllm.LLMResponse(
            content="native text", usage=_USAGE, model="test", stop_reason="end_turn"
        )

    async def invoke_stream(
        self, _messages: list[Any], tools: list[Any] | None = None, **_kwargs: Any
    ) -> AsyncIterator[arcllm.Delta]:
        self.stream_calls += 1
        if self.stream_calls == 1:
            self.first_delta.set()
            yield arcllm.Delta(text="native ")
            if self.release is not None:
                await self.release.wait()
            yield arcllm.Delta(text="text")
            yield arcllm.Delta(
                usage=arcllm.Usage(input_tokens=1, output_tokens=2, total_tokens=3),
                stop_reason="end_turn",
            )


class _ToolStreamModel(_NativeModel):
    async def invoke_stream(
        self, _messages: list[Any], tools: list[Any] | None = None, **_kwargs: Any
    ) -> AsyncIterator[arcllm.Delta]:
        self.stream_calls += 1
        if self.stream_calls == 1:
            yield arcllm.Delta(tool_call=arcllm.ToolCallDelta(index=0, id="call", name="echo"))
            yield arcllm.Delta(
                tool_call=arcllm.ToolCallDelta(index=0, arguments='{"value":"secret"}')
            )
            yield arcllm.Delta(stop_reason="tool_use")
            return
        yield arcllm.Delta(text="complete")
        yield arcllm.Delta(stop_reason="end_turn")


class _BlockedStreamModel(_NativeModel):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def invoke_stream(
        self, _messages: list[Any], **_kwargs: Any
    ) -> AsyncIterator[arcllm.Delta]:
        self.started.set()
        try:
            await asyncio.Event().wait()
            yield arcllm.Delta(text="unreachable")
        finally:
            self.closed.set()


class _TextThenBlockedModel(_BlockedStreamModel):
    async def invoke_stream(
        self, _messages: list[Any], **_kwargs: Any
    ) -> AsyncIterator[arcllm.Delta]:
        try:
            yield arcllm.Delta(text="visible")
            self.started.set()
            await asyncio.Event().wait()
            yield arcllm.Delta(tool_call=arcllm.ToolCallDelta(index=0, id="late", name="echo"))
        finally:
            self.closed.set()


@pytest.mark.asyncio
async def test_native_text_arrives_before_provider_completion_and_is_monotonic() -> None:
    release = asyncio.Event()
    model = _NativeModel(release)
    counter = {"calls": 0}
    stream = await run_stream(
        model=model, capabilities=StaticProvider([_tool(counter)]), system_prompt="sys", task="task"
    )
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
    stream = await run_stream(
        model=_ToolStreamModel(),
        capabilities=StaticProvider([_tool(counter)]),
        system_prompt="sys",
        task="task",
    )
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
        await run_stream(
            model=streamed_model,
            capabilities=StaticProvider([_tool(counter)]),
            system_prompt="sys",
            task="task",
        )
    )
    blocking_model = _NativeModel()
    blocking = await run(blocking_model, StaticProvider([_tool(counter)]), "sys", "task")
    assert streamed.content == blocking.content == "native text"
    assert streamed_model.stream_calls == 1
    assert streamed_model.invoke_calls == 1


@pytest.mark.asyncio
async def test_cancel_closes_a_blocked_provider_before_its_first_delta() -> None:
    model = _BlockedStreamModel()
    counter = {"calls": 0}
    handles: list[Any] = []
    stream = await run_stream(
        model=model,
        capabilities=StaticProvider([_tool(counter)]),
        system_prompt="sys",
        task="task",
        on_handle=handles.append,
    )
    drain = asyncio.create_task(collect(stream))
    await asyncio.wait_for(model.started.wait(), timeout=1)
    await handles[0].cancel("did:arc:operator", reason="stop")
    result = await asyncio.wait_for(drain, timeout=1)
    await asyncio.wait_for(model.closed.wait(), timeout=1)
    assert result.completion_payload is not None
    assert result.completion_payload["error"] == "cancelled"
    assert counter["calls"] == 0


@pytest.mark.asyncio
async def test_cancel_after_text_closes_stream_before_tool_fragment_dispatch() -> None:
    model = _TextThenBlockedModel()
    counter = {"calls": 0}
    handles: list[Any] = []
    stream = await run_stream(
        model=model,
        capabilities=StaticProvider([_tool(counter)]),
        system_prompt="sys",
        task="task",
        on_handle=handles.append,
    )
    iterator = stream.__aiter__()
    first = await asyncio.wait_for(anext(iterator), timeout=1)
    assert isinstance(first, TokenEvent)
    await asyncio.wait_for(model.started.wait(), timeout=1)
    await handles[0].cancel("did:arc:operator", reason="stop")
    rest = [event async for event in iterator]
    await asyncio.wait_for(model.closed.wait(), timeout=1)
    assert counter["calls"] == 0
    assert sum(isinstance(event, TurnEndEvent) for event in rest) == 1
    assert isinstance(rest[-1], TurnEndEvent)


@pytest.mark.asyncio
async def test_cancelling_after_terminal_is_idempotent() -> None:
    handles: list[Any] = []
    counter = {"calls": 0}
    events = [
        event
        async for event in await run_stream(
            model=_NativeModel(),
            capabilities=StaticProvider([_tool(counter)]),
            system_prompt="sys",
            task="task",
            on_handle=handles.append,
        )
    ]
    await handles[0].cancel("did:arc:operator")
    await handles[0].cancel("did:arc:operator")
    assert sum(isinstance(event, TurnEndEvent) for event in events) == 1
