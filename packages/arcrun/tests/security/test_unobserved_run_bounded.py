"""A run nobody is watching is still bounded.

A browser leaving no longer cancels the run it started, so the run's own caps
are the only thing that stops a runaway turn. They must hold with zero
observers: no ``on_event`` consumer, no stream reader, no socket.
"""

from __future__ import annotations

import asyncio

import pytest
from packages.arcrun.tests.conftest import LLMResponse, MockModel, ToolCall

from arcrun import StaticProvider
from arcrun.loop import run_async
from arcrun.types import Tool

_MAX_TURNS = 3


async def _echo(params: dict, ctx: object) -> str:
    return "echo"


def _tools() -> list[Tool]:
    return [
        Tool(
            name="echo",
            description="Echo",
            input_schema={"type": "object", "properties": {}},
            execute=_echo,
        )
    ]


def _endless_tool_use_model() -> MockModel:
    """A model that never stops asking for another tool call."""
    return MockModel(
        [
            LLMResponse(
                tool_calls=[ToolCall(id=f"tc{i}", name="echo", arguments={})],
                stop_reason="tool_use",
            )
            for i in range(50)
        ]
    )


@pytest.mark.asyncio
async def test_unobserved_run_stops_at_max_turns() -> None:
    handle = await run_async(
        _endless_tool_use_model(),
        StaticProvider(_tools()),
        "prompt",
        "task",
        max_turns=_MAX_TURNS,
    )

    result = await asyncio.wait_for(handle.result(), timeout=10)

    assert result.turns <= _MAX_TURNS
    assert result.completion_payload is not None
    assert result.completion_payload["error"] == "max_turns"
    assert not handle.state.cancelled_by, "the cap ended it, not an observer"
