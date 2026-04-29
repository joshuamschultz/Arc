"""Unit tests for TranscriptView.

Verifies:
- Delta accumulation under interleaved token arrivals produces stable output.
- MessageRole normalisation (string → enum).
- Markdown stripping removes **bold**, *italic*, `code`.
- start_streaming / append_delta / finish_streaming state machine.
- clear() resets all state.

These tests use Textual's ``App`` pilot harness so the widget can be
mounted and interacted with without a real terminal.
"""

from __future__ import annotations

import pytest

from arctui.transcript import MessageRole, TranscriptMessage, TranscriptView, _strip_markdown

# ---------------------------------------------------------------------------
# Pure-function tests (no Textual App required)
# ---------------------------------------------------------------------------


class TestStripMarkdown:
    """Tests for the ``_strip_markdown`` helper."""

    def test_bold_removed(self) -> None:
        """**text** → text."""
        assert _strip_markdown("**hello**") == "hello"

    def test_italic_removed(self) -> None:
        """*text* → text."""
        assert _strip_markdown("*hello*") == "hello"

    def test_inline_code_removed(self) -> None:
        """`text` → text."""
        assert _strip_markdown("`hello`") == "hello"

    def test_mixed_inline(self) -> None:
        """Mixed inline markers all stripped."""
        result = _strip_markdown("**bold** *italic* `code`")
        assert result == "bold italic code"

    def test_plain_text_unchanged(self) -> None:
        """Text without markers is unchanged."""
        assert _strip_markdown("hello world") == "hello world"

    def test_multiline_unchanged(self) -> None:
        """Block-level elements pass through without modification."""
        text = "# Heading\n\n- list item\n- another"
        assert _strip_markdown(text) == text

    def test_empty_string(self) -> None:
        """Empty string returns empty string."""
        assert _strip_markdown("") == ""


class TestTranscriptMessage:
    """Tests for the TranscriptMessage dataclass."""

    def test_default_not_streaming(self) -> None:
        """Messages default to is_streaming=False."""
        msg = TranscriptMessage(role=MessageRole.USER, content="hi")
        assert msg.is_streaming is False

    def test_streaming_flag(self) -> None:
        """is_streaming can be set True."""
        msg = TranscriptMessage(role=MessageRole.ASSISTANT, content="", is_streaming=True)
        assert msg.is_streaming is True


class TestMessageRole:
    """Tests for the MessageRole enum."""

    def test_user_value(self) -> None:
        """USER has value 'user'."""
        assert MessageRole.USER.value == "user"

    def test_assistant_value(self) -> None:
        """ASSISTANT has value 'assistant'."""
        assert MessageRole.ASSISTANT.value == "assistant"

    def test_from_string(self) -> None:
        """MessageRole can be constructed from its string value."""
        assert MessageRole("user") is MessageRole.USER
        assert MessageRole("assistant") is MessageRole.ASSISTANT


# ---------------------------------------------------------------------------
# Widget behaviour tests (using Textual App pilot)
# ---------------------------------------------------------------------------


class _TranscriptApp:
    """Minimal Textual App containing only a TranscriptView for testing.

    We build a simple wrapper that Textual's ``pilot`` can drive.
    """


@pytest.mark.asyncio
async def test_add_message_appended() -> None:
    """add_message records the message in internal state."""
    from textual.app import App, ComposeResult

    class TestApp(App[None]):
        def compose(self) -> ComposeResult:
            yield TranscriptView(id="tv")

    async with TestApp().run_test() as pilot:
        tv = pilot.app.query_one("#tv", TranscriptView)
        tv.add_message(MessageRole.USER, "hello")
        assert len(tv._messages) == 1
        assert tv._messages[0].content == "hello"
        assert tv._messages[0].role == MessageRole.USER


@pytest.mark.asyncio
async def test_streaming_delta_accumulation() -> None:
    """Delta tokens accumulate correctly under interleaved calls."""
    from textual.app import App, ComposeResult

    class TestApp(App[None]):
        def compose(self) -> ComposeResult:
            yield TranscriptView(id="tv")

    async with TestApp().run_test() as pilot:
        tv = pilot.app.query_one("#tv", TranscriptView)
        idx = tv.start_streaming(MessageRole.ASSISTANT)

        tokens = ["Hello", " world", "!", " How", " are", " you?"]
        for token in tokens:
            tv.append_delta(token)

        expected = "".join(tokens)
        assert tv._messages[idx].content == expected
        assert tv._messages[idx].is_streaming is True

        tv.finish_streaming()
        assert tv._messages[idx].is_streaming is False
        assert tv._streaming_idx is None


@pytest.mark.asyncio
async def test_streaming_with_interleaved_messages() -> None:
    """Interleaved non-streaming messages don't corrupt streaming state."""
    from textual.app import App, ComposeResult

    class TestApp(App[None]):
        def compose(self) -> ComposeResult:
            yield TranscriptView(id="tv")

    async with TestApp().run_test() as pilot:
        tv = pilot.app.query_one("#tv", TranscriptView)

        # Start streaming
        idx = tv.start_streaming(MessageRole.ASSISTANT)
        tv.append_delta("Part 1")

        # Add a system message (should not interfere with streaming)
        tv.add_message(MessageRole.SYSTEM, "note")

        # Continue streaming
        tv.append_delta(" Part 2")
        tv.finish_streaming()

        assert tv._messages[idx].content == "Part 1 Part 2"
        # System message is separate
        assert any(m.role == MessageRole.SYSTEM for m in tv._messages)


@pytest.mark.asyncio
async def test_clear_removes_all_messages() -> None:
    """clear() removes all messages and resets state."""
    from textual.app import App, ComposeResult

    class TestApp(App[None]):
        def compose(self) -> ComposeResult:
            yield TranscriptView(id="tv")

    async with TestApp().run_test() as pilot:
        tv = pilot.app.query_one("#tv", TranscriptView)
        tv.add_message(MessageRole.USER, "msg1")
        tv.add_message(MessageRole.ASSISTANT, "msg2")
        assert len(tv._messages) == 2

        tv.clear()
        assert len(tv._messages) == 0
        assert len(tv._labels) == 0
        assert tv._streaming_idx is None


@pytest.mark.asyncio
async def test_string_role_normalised() -> None:
    """String role values are normalised to MessageRole enum."""
    from textual.app import App, ComposeResult

    class TestApp(App[None]):
        def compose(self) -> ComposeResult:
            yield TranscriptView(id="tv")

    async with TestApp().run_test() as pilot:
        tv = pilot.app.query_one("#tv", TranscriptView)
        tv.add_message("user", "hi")
        assert tv._messages[0].role == MessageRole.USER


@pytest.mark.asyncio
async def test_append_delta_no_op_when_not_streaming() -> None:
    """append_delta is a no-op when no streaming is active."""
    from textual.app import App, ComposeResult

    class TestApp(App[None]):
        def compose(self) -> ComposeResult:
            yield TranscriptView(id="tv")

    async with TestApp().run_test() as pilot:
        tv = pilot.app.query_one("#tv", TranscriptView)
        # No stream started — should not raise
        tv.append_delta("should be ignored")
        assert len(tv._messages) == 0
