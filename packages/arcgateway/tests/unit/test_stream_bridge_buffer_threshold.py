"""Tests for StreamBridge.buffer_threshold — SSE-friendly unbuffered mode.

In unbuffered mode (buffer_threshold=0), every token delta should trigger
its own edit call. The default Slack/Telegram behaviour batches edits to
avoid hitting per-message rate limits; FastAPI SSE clients want the
unbatched stream so the visitor sees real-time tokens.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from arcgateway.delivery import DeliveryTarget
from arcgateway.executor import Delta
from arcgateway.stream_bridge import StreamBridge


class _RecordingAdapter:
    """Captures every send / edit / send_with_id call."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self._counter = 0

    async def send(self, target, message, *, reply_to=None) -> None:  # type: ignore[no-untyped-def]
        self.events.append(("send", message))

    async def send_with_id(self, target, message) -> str:  # type: ignore[no-untyped-def]
        self._counter += 1
        self.events.append(("send_with_id", message))
        return f"msg-{self._counter}"

    async def edit_message(self, target, message_id, text) -> None:  # type: ignore[no-untyped-def]
        self.events.append(("edit", text))


async def _stream(chunks: list[str]) -> AsyncIterator[Delta]:
    for c in chunks:
        yield Delta(kind="token", content=c, is_final=False)
    yield Delta(kind="done", content="", is_final=True)


@pytest.mark.asyncio
async def test_unbuffered_mode_edits_per_token() -> None:
    bridge = StreamBridge(buffer_threshold=0)
    adapter = _RecordingAdapter()
    target = DeliveryTarget(platform="python", chat_id="abc", thread_id=None)

    await bridge.consume(_stream(["he", "llo", "!"]), target, adapter)

    edits = [text for kind, text in adapter.events if kind == "edit"]
    # Each token triggers its own edit (cumulative accumulated content).
    assert edits == ["he", "hello", "hello!"]


@pytest.mark.asyncio
async def test_default_mode_batches_tokens() -> None:
    bridge = StreamBridge()  # default buffer_threshold = EDIT_TOKEN_BUFFER_SIZE
    adapter = _RecordingAdapter()
    target = DeliveryTarget(platform="python", chat_id="abc", thread_id=None)

    # Only a handful of tokens — well under the default 20-token threshold
    # AND too quick to hit the 1.5s elapsed-time flush. No edits expected;
    # the final-send delivers the whole content.
    await bridge.consume(_stream(["he", "llo", "!"]), target, adapter)

    edits = [text for kind, text in adapter.events if kind == "edit"]
    sends = [text for kind, text in adapter.events if kind == "send"]
    assert edits == []
    assert sends == ["hello!"]


def test_negative_threshold_rejected() -> None:
    with pytest.raises(ValueError, match="buffer_threshold"):
        StreamBridge(buffer_threshold=-1)
