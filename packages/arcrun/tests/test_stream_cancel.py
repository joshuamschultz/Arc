"""GAP-A: a streaming run exposes a cancellable handle to its caller.

``run_stream`` drives the same ReAct loop as the blocking path, but until now
its internal ``RunHandle`` was unreachable — so the operator kill-switch (the
runcontrol watcher) could only cancel *tracked* runs, never chat/streaming ones.

These tests pin the fix: ``run_stream(on_handle=...)`` hands the live handle to
the caller, and cancelling it routes the stream through the SAME
``cancel_event`` + ``_halt_on_cancel`` terminator the tracked path already uses
— a structured, operator-attributed cancelled result. A run that is never
cancelled must behave EXACTLY as before (no-regression).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from conftest import LLMResponse, MockModel, ToolCall

from arcrun import StaticProvider, StreamEvent, TokenEvent, TurnEndEvent, run_stream
from arcrun.loop import RunHandle
from arcrun.types import Tool


async def _slow_echo(params: dict[str, Any], ctx: object) -> str:
    await asyncio.sleep(0.01)
    return f"echo: {params.get('input', '')}"


def _tools() -> list[Tool]:
    return [
        Tool(
            name="echo",
            description="Echo",
            input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
            execute=_slow_echo,
        )
    ]


def _looping_model() -> MockModel:
    """A model that keeps calling a tool — only a cancel/breaker stops it."""
    return MockModel(
        [
            LLMResponse(
                tool_calls=[ToolCall(id=f"tc{i}", name="echo", arguments={"input": "x"})],
                stop_reason="tool_use",
            )
            for i in range(50)
        ]
    )


async def _drain(stream: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [ev async for ev in stream]


@pytest.mark.asyncio
async def test_run_stream_exposes_handle_that_cancels() -> None:
    """The handle handed to ``on_handle`` cooperatively stops the streaming loop.

    Cancelling mid-stream terminates the run through the shared cancel terminator:
    the terminal ``TurnEndEvent`` carries the ``error="cancelled"`` structured
    payload and a human-visible partial naming the operator (ASI09/ASI10).
    """
    captured: dict[str, RunHandle] = {}

    def _on_handle(handle: RunHandle) -> None:
        captured["handle"] = handle

    stream = await run_stream(
        model=_looping_model(),
        capabilities=StaticProvider(_tools()),
        system_prompt="prompt",
        task="task",
        max_turns=50,
        on_handle=_on_handle,
    )

    events: list[StreamEvent] = []
    cancelled = False
    async for ev in stream:
        events.append(ev)
        # Cancel as soon as the handle is available and the loop is progressing.
        if not cancelled and "handle" in captured:
            cancelled = True
            await captured["handle"].cancel("did:arc:operator", reason="taking too long")

    assert cancelled, "handle was never exposed to on_handle"
    turn_end = events[-1]
    assert isinstance(turn_end, TurnEndEvent)
    assert turn_end.completion_payload is not None
    assert turn_end.completion_payload["error"] == "cancelled"
    assert "did:arc:operator" in turn_end.final_text
    assert "taking too long" in turn_end.final_text
    # The loop stopped early — a cancelled run must not exhaust all 50 turns.
    assert turn_end.turns < 50


@pytest.mark.asyncio
async def test_uncancelled_stream_matches_no_on_handle() -> None:
    """No-regression: wiring ``on_handle`` changes nothing for an uncancelled run.

    The exact same model streamed with ``on_handle=None`` and with a no-op
    ``on_handle`` must yield an identical event sequence and final result — the
    handle seam is inert unless the caller actually cancels.
    """

    def _model() -> MockModel:
        return MockModel(
            [
                LLMResponse(
                    tool_calls=[ToolCall(id="t1", name="echo", arguments={"input": "hi"})],
                    stop_reason="tool_use",
                ),
                LLMResponse(content="final answer", stop_reason="end_turn"),
            ]
        )

    def _kinds(events: list[StreamEvent]) -> list[str]:
        return [type(e).__name__ for e in events]

    baseline = await _drain(
        await run_stream(
            model=_model(),
            capabilities=StaticProvider(_tools()),
            system_prompt="prompt",
            task="task",
            max_turns=10,
        )
    )
    with_handle = await _drain(
        await run_stream(
            model=_model(),
            capabilities=StaticProvider(_tools()),
            system_prompt="prompt",
            task="task",
            max_turns=10,
            on_handle=lambda _h: None,
        )
    )

    assert _kinds(baseline) == _kinds(with_handle)
    b_end, h_end = baseline[-1], with_handle[-1]
    assert isinstance(b_end, TurnEndEvent) and isinstance(h_end, TurnEndEvent)
    assert b_end.final_text == h_end.final_text == "final answer"
    assert b_end.completion_payload is None and h_end.completion_payload is None
    assert b_end.turns == h_end.turns
    assert any(isinstance(e, TokenEvent) for e in with_handle)
