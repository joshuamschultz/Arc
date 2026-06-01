"""run_stream() must forward the same execution context as run().

The streaming entry is the single path an agent turn takes (SPEC-027), so it
has to carry everything the blocking loop carried: prior ``messages`` (history
parity), an external ``on_event`` bridge (SPEC-026 recording + module
telemetry ride this), ``transform_context``, and ``tool_choice``. These are
additive; omitting them preserves the original behavior.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from arcrun import StaticProvider, TurnEndEvent, run_stream
from arcrun.events import Event
from arcrun.types import LoopResult, Tool


def _tool() -> Tool:
    async def _execute(args: dict[str, Any], ctx: Any) -> str:
        return "ok"

    return Tool(
        name="noop",
        description="noop",
        input_schema={"type": "object", "properties": {}, "required": []},
        execute=_execute,
    )


@pytest.mark.asyncio
async def test_run_stream_forwards_messages_bridge_and_tool_choice() -> None:
    """run_stream forwards messages/on_event/transform_context/tool_choice to the loop."""
    captured: dict[str, Any] = {}
    external_events: list[Event] = []

    async def _fake_run(*args: Any, **kwargs: Any) -> LoopResult:
        captured.update(kwargs)
        # Simulate the loop pushing an event through the bridge it was handed.
        on_event = kwargs.get("on_event")
        if on_event is not None:
            on_event(Event(type="tool.start", timestamp=1.0, run_id="r", data={"name": "noop"}))
        return LoopResult(
            content="done",
            turns=1,
            tool_calls_made=1,
            tokens_used={},
            strategy_used="react",
            cost_usd=0.0,
        )

    history = [{"role": "user", "content": "earlier turn"}]

    def _bridge(event: Event) -> None:
        external_events.append(event)

    def _transform(messages: list[Any]) -> list[Any]:
        return messages

    with patch("arcrun.loop.run", side_effect=_fake_run):
        stream = await run_stream(
            model=object(),
            capabilities=StaticProvider([_tool()]),
            system_prompt="sys",
            task="do it",
            messages=history,
            on_event=_bridge,
            transform_context=_transform,
            tool_choice={"type": "any"},
        )
        events = [ev async for ev in stream]

    assert captured["messages"] == history
    assert captured["transform_context"] is _transform
    assert captured["tool_choice"] == {"type": "any"}
    # The external bridge received the loop event (recording path intact).
    assert any(e.type == "tool.start" for e in external_events)
    # Stream still terminates with a TurnEndEvent.
    assert isinstance(events[-1], TurnEndEvent)
    assert events[-1].final_text == "done"
