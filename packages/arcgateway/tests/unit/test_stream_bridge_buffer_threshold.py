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
    # AND too quick to hit the 1.5s elapsed-time flush. No mid-stream edits;
    # the whole content is delivered by a single final in-place edit of the
    # placeholder (NOT a second `send`, which would duplicate the reply).
    await bridge.consume(_stream(["he", "llo", "!"]), target, adapter)

    edits = [text for kind, text in adapter.events if kind == "edit"]
    sends = [text for kind, text in adapter.events if kind == "send"]
    assert edits == ["hello!"]
    assert sends == []


@pytest.mark.asyncio
async def test_editable_adapter_finalizes_in_place_no_duplicate() -> None:
    """Edit-capable adapters must NOT receive a second `send` for the final text.

    Regression for the Telegram double-reply bug: the bridge streamed
    progressive edits into the placeholder AND then sent a brand-new message
    with the same text, so every reply appeared twice. The final delivery
    must be an in-place edit, never a duplicate send.
    """
    bridge = StreamBridge()  # default threshold — short stream, no mid-stream flush
    adapter = _RecordingAdapter()
    target = DeliveryTarget(platform="telegram", chat_id="42", thread_id=None)

    await bridge.consume(_stream(["he", "llo", "!"]), target, adapter)

    sends = [text for kind, text in adapter.events if kind == "send"]
    edits = [text for kind, text in adapter.events if kind == "edit"]
    assert sends == [], f"final text must be edited in place, not re-sent; got sends={sends}"
    assert edits[-1] == "hello!", f"final edit must carry the full text; got {edits}"


class _SendOnlyAdapter:
    """Adapter with no edit_message — models web / in-process transports."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    async def send(self, target, message, *, reply_to=None) -> None:  # type: ignore[no-untyped-def]
        self.events.append(("send", message))

    async def send_with_id(self, target, message) -> str | None:  # type: ignore[no-untyped-def]
        self.events.append(("send_with_id", message))
        return None


class _IncrementalAdapter:
    """Send-only transport that exposes ordered delta delivery."""

    def __init__(self) -> None:
        self.deltas: list[Delta] = []

    async def send(self, target, message, *, reply_to=None) -> None:  # type: ignore[no-untyped-def]
        raise AssertionError("incremental transport must not receive final send")

    async def send_delta(self, target, delta: Delta) -> None:  # type: ignore[no-untyped-def]
        self.deltas.append(delta)


@pytest.mark.asyncio
async def test_send_only_adapter_skips_placeholder_single_message() -> None:
    """Send-only adapters get exactly one message and no dangling placeholder."""
    bridge = StreamBridge()
    adapter = _SendOnlyAdapter()
    target = DeliveryTarget(platform="web", chat_id="abc", thread_id=None)

    await bridge.consume(_stream(["he", "llo", "!"]), target, adapter)

    assert adapter.events == [("send", "hello!")], (
        f"expected a single final send and no placeholder; got {adapter.events}"
    )


@pytest.mark.asyncio
async def test_incremental_send_only_adapter_receives_ordered_tokens_and_terminal() -> None:
    """Send-only adapters can opt into progressive ordered delta delivery."""
    bridge = StreamBridge()
    adapter = _IncrementalAdapter()
    target = DeliveryTarget(platform="python", chat_id="abc", thread_id=None)

    await bridge.consume(_stream(["he", "llo", "!"]), target, adapter)

    assert [delta.content for delta in adapter.deltas] == ["he", "llo", "!", ""]
    assert [delta.kind for delta in adapter.deltas] == ["token", "token", "token", "done"]
    assert adapter.deltas[-1].is_final is True


class _SplittingAdapter:
    """Edit-capable adapter with a small per-message limit, like Telegram/Slack."""

    LIMIT = 10

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    async def send(self, target, message, *, reply_to=None) -> None:  # type: ignore[no-untyped-def]
        self.events.append(("send", message))

    async def send_with_id(self, target, message) -> str:  # type: ignore[no-untyped-def]
        self.events.append(("send_with_id", message))
        return "mid-1"

    async def edit_message(self, target, message_id, text) -> None:  # type: ignore[no-untyped-def]
        self.events.append(("edit", text))

    # Declared, not implemented: the gateway owns splitting (REQ-310).
    max_message_chars = LIMIT


@pytest.mark.asyncio
async def test_long_reply_splits_first_chunk_edited_rest_sent() -> None:
    """A reply over the platform limit edits chunk 1 in place and sends the rest.

    No chunk is duplicated: chunk 0 lands via edit_message, chunks 1..n via send.
    """
    bridge = StreamBridge()
    adapter = _SplittingAdapter()
    target = DeliveryTarget(platform="telegram", chat_id="42", thread_id=None)

    # 25 chars → 3 chunks of 10/10/5 under the adapter's LIMIT of 10.
    full = "ABCDEFGHIJKLMNOPQRSTUVWXY"
    await bridge.consume(_stream([full]), target, adapter)

    edits = [text for kind, text in adapter.events if kind == "edit"]
    sends = [text for kind, text in adapter.events if kind == "send"]
    assert edits == ["ABCDEFGHIJ"], f"first chunk must be edited in place; got {edits}"
    assert sends == ["KLMNOPQRST", "UVWXY"], f"overflow chunks must be sent; got {sends}"
    # Reassembled, the delivered text is exactly the reply — nothing dropped.
    assert (edits + sends) == ["ABCDEFGHIJ", "KLMNOPQRST", "UVWXY"]
    assert "".join(edits + sends) == full


def test_negative_threshold_rejected() -> None:
    with pytest.raises(ValueError, match="buffer_threshold"):
        StreamBridge(buffer_threshold=-1)
