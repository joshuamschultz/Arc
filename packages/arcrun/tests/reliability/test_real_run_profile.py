"""Cheap deterministic reliability profile through public ArcRun APIs."""

from __future__ import annotations

import asyncio
import math
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

import arcllm
import pytest

from arcrun import StaticProvider, TokenEvent, TurnEndEvent, run, run_stream
from arcrun.ledger import ToolExecutionIntent, ToolExecutionOutcome, ToolLedgerEntry
from arcrun.types import Tool


def _response(
    *,
    content: str | None = None,
    tool_calls: list[arcllm.ToolCall] | None = None,
    stop_reason: arcllm.StopReason = "end_turn",
) -> arcllm.LLMResponse:
    return arcllm.LLMResponse(
        content=content,
        tool_calls=tool_calls or [],
        usage=arcllm.Usage(input_tokens=1, output_tokens=1, total_tokens=2),
        model="scripted",
        stop_reason=stop_reason,
    )


class _EndModel:
    async def invoke(self, _messages: list[Any], **_kwargs: Any) -> arcllm.LLMResponse:
        return _response(content="ok")


class _PartialModel(_EndModel):
    async def invoke_stream(
        self, _messages: list[Any], **_kwargs: Any
    ) -> AsyncIterator[arcllm.Delta]:
        yield arcllm.Delta(text="partial")
        raise ConnectionError("simulated SSE disconnect")


class _BlockingStreamModel(_EndModel):
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.closed = asyncio.Event()

    async def invoke_stream(
        self, _messages: list[Any], **_kwargs: Any
    ) -> AsyncIterator[arcllm.Delta]:
        try:
            yield arcllm.Delta(text="partial")
            self.entered.set()
            await asyncio.Event().wait()
        finally:
            self.closed.set()


class _FloodStreamModel(_EndModel):
    def __init__(self) -> None:
        self.closed = asyncio.Event()

    async def invoke_stream(
        self, _messages: list[Any], **_kwargs: Any
    ) -> AsyncIterator[arcllm.Delta]:
        try:
            for _ in range(10):
                yield arcllm.Delta(text="flood")
            await asyncio.Event().wait()
        finally:
            self.closed.set()


class _ToolModel:
    def __init__(self) -> None:
        self.calls = 0

    async def invoke(self, _messages: list[Any], **_kwargs: Any) -> arcllm.LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return _response(
                tool_calls=[arcllm.ToolCall(id="write-1", name="write", arguments={})],
                stop_reason="tool_use",
            )
        return _response(content="ok")


@dataclass
class _Entry:
    status: Literal["new", "completed", "unresolved"]
    outcome: ToolExecutionOutcome | None = None


class _Ledger:
    def __init__(self) -> None:
        self.entries: dict[str, _Entry] = {}

    async def begin(self, intent: ToolExecutionIntent) -> ToolLedgerEntry:
        entry = self.entries.setdefault(intent.invocation_key, _Entry("new"))
        return ToolLedgerEntry(entry.status, entry.outcome)

    async def complete(self, outcome: ToolExecutionOutcome) -> None:
        self.entries[outcome.invocation_key] = _Entry("completed", outcome)


def _provider(counter: dict[str, int]) -> StaticProvider:
    async def _write(_args: dict[str, Any], _ctx: Any) -> str:
        counter["writes"] += 1
        return "written"

    return StaticProvider([Tool("write", "write", {"type": "object"}, _write)])


def _wilson_lower_bound(successes: int, total: int) -> float:
    z = 1.96
    p = successes / total
    return (p + z**2 / (2 * total) - z * math.sqrt(p * (1 - p) / total + z**2 / (4 * total**2))) / (
        1 + z**2 / total
    )


@pytest.mark.asyncio
async def test_default_real_run_reliability_profile() -> None:
    runs = await asyncio.wait_for(
        asyncio.gather(
            *(
                run(
                    _EndModel(),
                    _provider({"writes": 0}),
                    "sys",
                    "task",
                    allowed_strategies=["react"],
                )
                for _ in range(100)
            )
        ),
        timeout=5,
    )
    assert len(runs) == 100
    assert all(
        len([event for event in result.events if event.type == "loop.complete"]) == 1
        for result in runs
    )
    assert all(result.content == "ok" for result in runs)

    stream = await run_stream(
        model=_PartialModel(),
        capabilities=_provider({"writes": 0}),
        system_prompt="sys",
        task="task",
        allowed_strategies=["react"],
    )
    events = [event async for event in stream]
    assert any(isinstance(event, TokenEvent) for event in events)
    assert isinstance(events[-1], TurnEndEvent)
    assert events[-1].completion_payload is not None

    blocked_model = _BlockingStreamModel()
    blocked_stream = await run_stream(
        model=blocked_model,
        capabilities=_provider({"writes": 0}),
        system_prompt="sys",
        task="task",
        allowed_strategies=["react"],
        delivery_queue_size=1,
    )
    iterator = blocked_stream.__aiter__()
    assert isinstance(await anext(iterator), TokenEvent)
    await asyncio.wait_for(blocked_model.entered.wait(), timeout=1)
    await iterator.aclose()
    await asyncio.wait_for(blocked_model.closed.wait(), timeout=1)

    flood_model = _FloodStreamModel()
    flood_stream = await run_stream(
        model=flood_model,
        capabilities=_provider({"writes": 0}),
        system_prompt="sys",
        task="task",
        allowed_strategies=["react"],
        delivery_queue_size=1,
    )
    flood_iterator = flood_stream.__aiter__()
    assert isinstance(await anext(flood_iterator), TokenEvent)
    await asyncio.wait_for(flood_model.closed.wait(), timeout=1)
    await flood_iterator.aclose()

    counter = {"writes": 0}
    ledger = _Ledger()
    first = await run(
        _ToolModel(),
        _provider(counter),
        "sys",
        "task",
        allowed_strategies=["react"],
        run_id="replay",
        tool_ledger=ledger,
    )
    replay = await run(
        _ToolModel(),
        _provider(counter),
        "sys",
        "task",
        allowed_strategies=["react"],
        run_id="replay",
        tool_ledger=ledger,
    )
    assert first.content == replay.content == "ok"
    assert counter["writes"] == 1
    assert first.tool_calls_made == replay.tool_calls_made == 1
    assert all(
        len([event for event in result.events if event.type == "loop.complete"]) == 1
        for result in (first, replay)
    )


@pytest.mark.slow
@pytest.mark.asyncio
async def test_10k_real_run_slo_profile() -> None:
    count = int(os.environ.get("ARCRUN_RELIABILITY_RUNS", "10000"))
    batch_size = 100
    completed = 0
    for offset in range(0, count, batch_size):
        batch = min(batch_size, count - offset)
        results = await asyncio.gather(
            *(
                run(
                    _EndModel(),
                    _provider({"writes": 0}),
                    "sys",
                    "task",
                    allowed_strategies=["react"],
                )
                for _ in range(batch)
            )
        )
        completed += sum(
            result.content == "ok"
            and len([event for event in result.events if event.type == "loop.complete"]) == 1
            for result in results
        )
    assert completed == count
    assert _wilson_lower_bound(completed, count) >= 0.99
