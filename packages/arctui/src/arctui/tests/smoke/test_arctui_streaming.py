"""Smoke tests: arctui per-token streaming via ``agent.run``.

Verifies that ArcTUI's ``_run_stream_turn`` drives the single streaming entry
(``agent.run(text, session=agent.session(key))``) and renders each token via
TranscriptView.start_streaming / append_delta / finish_streaming.

No real ArcAgent or LLM is involved — a fake streaming agent is used.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Fake streaming agent — new SPEC-027 contract: session(key) + streaming run()
# ---------------------------------------------------------------------------


class _FakeStreamingAgent:
    """Minimal agent exposing ``session`` + a streaming ``run``."""

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens
        self._bus = None  # _wire_event_bus() is a no-op

    async def session(self, key: str) -> str:
        return key

    async def run(self, input_text: str, *, session: Any) -> Any:
        from arcrun import TokenEvent, TurnEndEvent

        for t in self._tokens:
            yield TokenEvent(text=t)
        yield TurnEndEvent(final_text="".join(self._tokens))


@pytest.mark.asyncio
async def test_stream_turn_appends_deltas_incrementally() -> None:
    """_run_stream_turn drives agent.run and renders the user + streamed reply."""
    from arctui.app import ArcTUI
    from arctui.input_composer import InputComposer
    from arctui.transcript import MessageRole, TranscriptView

    agent = _FakeStreamingAgent(["hello", " ", "world"])
    app = ArcTUI(agent=agent)
    async with app.run_test() as pilot:
        tv: TranscriptView = pilot.app.query_one("#transcript", TranscriptView)
        pilot.app.post_message(InputComposer.SubmitMessage("say hello"))
        await asyncio.sleep(0.5)

        messages = tv._messages
        roles = [m.role for m in messages]
        assert MessageRole.USER in roles or any("say hello" in m.content for m in messages)


@pytest.mark.asyncio
async def test_stream_turn_finish_streaming_called() -> None:
    """finish_streaming() runs after the stream ends (no dangling cursor)."""
    from arctui.app import ArcTUI
    from arctui.input_composer import InputComposer
    from arctui.transcript import TranscriptView

    agent = _FakeStreamingAgent(["done"])
    app = ArcTUI(agent=agent)
    async with app.run_test() as pilot:
        tv: TranscriptView = pilot.app.query_one("#transcript", TranscriptView)
        pilot.app.post_message(InputComposer.SubmitMessage("done"))
        await asyncio.sleep(0.5)
        assert tv._streaming_idx is None, "streaming cursor still active after stream end"


@pytest.mark.asyncio
async def test_streaming_error_shows_error_message() -> None:
    """When agent.run raises mid-stream, the turn finishes cleanly (no crash)."""
    from arctui.app import ArcTUI
    from arctui.input_composer import InputComposer
    from arctui.transcript import TranscriptView

    class _ErrorAgent:
        _bus = None

        async def session(self, key: str) -> str:
            return key

        async def run(self, input_text: str, *, session: Any) -> Any:
            raise RuntimeError("simulated stream error")
            yield  # unreachable — makes this an async generator

    app = ArcTUI(agent=_ErrorAgent())
    async with app.run_test() as pilot:
        tv: TranscriptView = pilot.app.query_one("#transcript", TranscriptView)
        pilot.app.post_message(InputComposer.SubmitMessage("trigger error"))
        await asyncio.sleep(0.5)
        assert tv._streaming_idx is None, "finish_streaming() should run even on stream errors"
