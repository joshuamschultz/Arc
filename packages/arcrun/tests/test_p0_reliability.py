"""P0 run lifecycle boundaries: selection, deadline, and stream failure."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import arcllm
import pytest

from arcrun import StaticProvider, TurnEndEvent, run, run_stream
from arcrun.types import Tool


def _provider() -> StaticProvider:
    async def _tool(_args: dict[str, Any], _ctx: Any) -> str:
        return "ok"

    return StaticProvider(
        [
            Tool(
                name="echo",
                description="echo",
                input_schema={"type": "object", "properties": {}},
                execute=_tool,
            )
        ]
    )


class _BlockedSelectionModel:
    async def invoke(self, _messages: list[Any], tools: list[Any] | None = None, **_kwargs: Any) -> Any:
        if tools:
            await asyncio.Event().wait()
        return arcllm.LLMResponse(content="never", stop_reason="end_turn")


class _PartialFailureModel:
    async def invoke(self, _messages: list[Any], tools: list[Any] | None = None, **_kwargs: Any) -> Any:
        if tools:
            return arcllm.LLMResponse(
                tool_calls=[arcllm.ToolCall(id="strategy", name="select_strategy", arguments={"strategy": "react"})],
                stop_reason="tool_use",
            )
        return arcllm.LLMResponse(content="never", stop_reason="end_turn")

    async def invoke_stream(self, _messages: list[Any], **_kwargs: Any) -> AsyncIterator[arcllm.Delta]:
        yield arcllm.Delta(text="partial")
        raise RuntimeError("wire lost")


@pytest.mark.asyncio
async def test_selection_deadline_returns_one_typed_terminal() -> None:
    result = await run(
        _BlockedSelectionModel(),
        _provider(),
        "system",
        "task",
        deadline=time.monotonic() + 0.02,
    )

    terminals = [event for event in result.events if event.type == "loop.complete"]
    assert len(terminals) == 1
    assert result.completion_payload is not None
    assert result.completion_payload["error"] == "deadline"


@pytest.mark.asyncio
async def test_partial_stream_failure_has_typed_terminal() -> None:
    stream = await run_stream(
        model=_PartialFailureModel(),
        capabilities=_provider(),
        system_prompt="system",
        task="task",
    )
    events = [event async for event in stream]

    assert events[-1].__class__ is TurnEndEvent
    terminal = events[-1]
    assert terminal.completion_payload is not None
    assert terminal.completion_payload["error"] == "provider_error"
