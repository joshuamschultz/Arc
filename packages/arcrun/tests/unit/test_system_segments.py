"""A segmented system prompt reaches the model as one message per segment.

A caller that knows which parts of its prompt change at different rates passes
them most-stable-first; arcrun must preserve that split (and its order) all the
way into the message list, because the provider adapter turns each segment into
its own cache breakpoint.
"""

from __future__ import annotations

from typing import Any

from arcrun import StaticProvider, Tool
from arcrun._messages import system_messages
from arcrun.loop import _build_state
from arcrun.types import ToolContext


async def _echo(args: dict[str, Any], ctx: ToolContext) -> str:
    return "ok"


def _provider() -> StaticProvider:
    return StaticProvider(
        [
            Tool(
                name="echo",
                description="Echo the text back.",
                input_schema={"type": "object", "properties": {}},
                execute=_echo,
            )
        ]
    )


def test_segments_become_one_system_message_each_in_order() -> None:
    state, _sandbox = _build_state(_provider(), ["session-stable", "run-stable"], "do it")

    system = [m for m in state.messages if m.role == "system"]
    assert [m.content for m in system] == ["session-stable", "run-stable"]
    assert state.messages[0].role == "system"
    assert state.messages[1].role == "system"


def test_plain_string_still_yields_one_system_message() -> None:
    state, _sandbox = _build_state(_provider(), "just one", "do it")

    assert [m.content for m in state.messages if m.role == "system"] == ["just one"]


def test_empty_segments_are_dropped() -> None:
    """An empty text block is invalid on the wire — never emit one."""
    assert system_messages(["", "real", ""]) == system_messages(["real"])
    assert system_messages("") == []
