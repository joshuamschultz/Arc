"""Smoke tests: arctui turn rendering via a ``ChatTransport``.

Verifies ArcTUI's ``_run_stream_turn`` drives ``transport.send_turn(text)`` and
renders each ``TurnEvent`` via TranscriptView.start_streaming / append_delta /
finish_streaming. No real ArcAgent, gateway, or LLM is involved — a fake
transport scripts the TurnEvent stream.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from arctui.transport import TurnEvent


class _FakeTransport:
    """Scripted ChatTransport: replays a fixed TurnEvent stream per turn."""

    def __init__(self, events: list[TurnEvent]) -> None:
        self._events = events
        self.closed = False

    async def send_turn(self, text: str) -> AsyncIterator[TurnEvent]:
        for ev in self._events:
            yield ev

    async def aclose(self) -> None:
        self.closed = True


class _RaisingTransport:
    """Transport whose turn raises mid-stream."""

    async def send_turn(self, text: str) -> AsyncIterator[TurnEvent]:
        raise RuntimeError("simulated transport error")
        yield  # unreachable — makes this an async generator

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_turn_appends_reply_text() -> None:
    """_run_stream_turn renders the user message + the streamed reply chunks."""
    from arctui.app import ArcTUI
    from arctui.input_composer import InputComposer
    from arctui.transcript import TranscriptView

    transport = _FakeTransport(
        [TurnEvent("message", "hello"), TurnEvent("message", " world"), TurnEvent("done")]
    )
    app = ArcTUI(transport=transport)
    async with app.run_test() as pilot:
        tv: TranscriptView = pilot.app.query_one("#transcript", TranscriptView)
        pilot.app.post_message(InputComposer.SubmitMessage("say hello"))
        await asyncio.sleep(0.5)

        texts = [m.content for m in tv._messages]
        assert any("say hello" in t for t in texts)
        assert any("hello world" in t for t in texts)


@pytest.mark.asyncio
async def test_turn_finish_streaming_called() -> None:
    """finish_streaming() runs after the turn ends (no dangling cursor)."""
    from arctui.app import ArcTUI
    from arctui.input_composer import InputComposer
    from arctui.transcript import TranscriptView

    transport = _FakeTransport([TurnEvent("message", "done"), TurnEvent("done")])
    app = ArcTUI(transport=transport)
    async with app.run_test() as pilot:
        tv: TranscriptView = pilot.app.query_one("#transcript", TranscriptView)
        pilot.app.post_message(InputComposer.SubmitMessage("done"))
        await asyncio.sleep(0.5)
        assert tv._streaming_idx is None, "streaming cursor still active after turn end"


@pytest.mark.asyncio
async def test_error_event_shows_error_message() -> None:
    """An error TurnEvent surfaces as an ERROR message and closes the turn."""
    from arctui.app import ArcTUI
    from arctui.input_composer import InputComposer
    from arctui.transcript import MessageRole, TranscriptView

    transport = _FakeTransport([TurnEvent("error", "boom"), TurnEvent("done")])
    app = ArcTUI(transport=transport)
    async with app.run_test() as pilot:
        tv: TranscriptView = pilot.app.query_one("#transcript", TranscriptView)
        pilot.app.post_message(InputComposer.SubmitMessage("trigger error"))
        await asyncio.sleep(0.5)
        assert tv._streaming_idx is None
        assert any(m.role == MessageRole.ERROR and "boom" in m.content for m in tv._messages)


@pytest.mark.asyncio
async def test_transport_exception_finishes_cleanly() -> None:
    """When send_turn raises, the turn finishes without crashing the app."""
    from arctui.app import ArcTUI
    from arctui.input_composer import InputComposer
    from arctui.transcript import TranscriptView

    app = ArcTUI(transport=_RaisingTransport())
    async with app.run_test() as pilot:
        tv: TranscriptView = pilot.app.query_one("#transcript", TranscriptView)
        pilot.app.post_message(InputComposer.SubmitMessage("trigger error"))
        await asyncio.sleep(0.5)
        assert tv._streaming_idx is None, "finish_streaming() should run even on transport errors"
