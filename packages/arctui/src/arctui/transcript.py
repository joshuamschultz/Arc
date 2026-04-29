"""TranscriptView — streamed message rendering for ArcTUI.

Displays the conversation turn by turn.  Accepts token deltas and appends
them to the current assistant message in-place so the user sees streaming
output without flicker.

Design constraints:
- No state shared with other widgets.  Thread-safe via Textual's reactive
  scheduling — all mutations go through ``call_from_thread`` or are posted
  as ``on_*`` messages from the asyncio loop.
- Markdown-lite: **bold**, *italic*, and `code` spans are stripped to ANSI
  equivalents via a minimal regex pass.  Full CommonMark is intentionally
  not supported here (it would require a markdown library dep); use the
  arcui web dashboard for rich rendering.
- Delta accumulation: if ``append_delta`` is called N times in a single
  event-loop tick, the underlying Text node is mutated once per call but
  only the last ``refresh()`` wins — Textual deduplicates repaints within
  a frame automatically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.widgets import Label, Static


class MessageRole(StrEnum):
    """Roles that can appear in the transcript."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"
    ERROR = "error"


@dataclass
class TranscriptMessage:
    """A single message in the transcript.

    Attributes
    ----------
    role:
        Speaker role.
    content:
        Accumulated text.  For assistant messages during streaming this
        grows one delta at a time.
    is_streaming:
        True while the assistant is still emitting tokens.
    """

    role: MessageRole
    content: str
    is_streaming: bool = False


# Minimal inline-markdown replacement table applied before display.
# Strips ``**bold**``, ``*italic*``, and ``\`code\``` to plain text.
# We keep this list explicit so it's easy to audit for injection (LLM07).
_MD_REPLACEMENTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\*\*(.+?)\*\*", re.DOTALL), r"\1"),  # **bold** -> text
    (re.compile(r"\*(.+?)\*", re.DOTALL), r"\1"),  # *italic* -> text
    (re.compile(r"`(.+?)`", re.DOTALL), r"\1"),  # `code` -> text
]


def _strip_markdown(text: str) -> str:
    """Remove lightweight inline markdown markers from *text*.

    Does NOT modify block-level elements (headings, lists) — those pass
    through verbatim.  This is intentionally conservative; richer rendering
    is out-of-scope (see module docstring).
    """
    for pattern, replacement in _MD_REPLACEMENTS:
        text = pattern.sub(replacement, text)
    return text


def _role_css_class(role: MessageRole) -> str:
    """Return the CSS class name for *role*."""
    return f"msg-{role.value}"


class TranscriptView(Static):
    """Scrollable transcript area showing conversation history.

    Methods
    -------
    add_message(role, content)
        Append a complete message.
    start_streaming(role)
        Begin a streaming message; returns the message index.
    append_delta(content)
        Append *content* to the current streaming message.
    finish_streaming()
        Mark the current streaming message complete.
    clear()
        Remove all messages.
    """

    DEFAULT_CSS: ClassVar[str] = """
    TranscriptView {
        height: 1fr;
        overflow-y: auto;
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._messages: list[TranscriptMessage] = []
        self._labels: list[Label] = []
        self._streaming_idx: int | None = None

    def compose(self) -> ComposeResult:
        """Empty on first render; messages are mounted dynamically."""
        # yield nothing — messages are mounted via mount() calls
        return
        yield

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_message(self, role: MessageRole | str, content: str) -> None:
        """Append a complete message to the transcript.

        Parameters
        ----------
        role:
            Speaker role.  Accepts ``MessageRole`` enum or plain string.
        content:
            Full message text.  Inline markdown is stripped before display.
        """
        if isinstance(role, str):
            try:
                role = MessageRole(role)
            except ValueError:
                role = MessageRole.SYSTEM
        msg = TranscriptMessage(role=role, content=content)
        self._messages.append(msg)
        label = self._make_label(msg)
        self._labels.append(label)
        self.mount(label)
        self.scroll_end(animate=False)

    def start_streaming(self, role: MessageRole | str = MessageRole.ASSISTANT) -> int:
        """Begin a streaming assistant message.

        Returns
        -------
        int
            Index of the new message in ``self._messages``.
        """
        if isinstance(role, str):
            try:
                role = MessageRole(role)
            except ValueError:
                role = MessageRole.ASSISTANT
        msg = TranscriptMessage(role=role, content="", is_streaming=True)
        self._messages.append(msg)
        label = self._make_label(msg)
        self._labels.append(label)
        self._streaming_idx = len(self._messages) - 1
        self.mount(label)
        self.scroll_end(animate=False)
        return self._streaming_idx

    def append_delta(self, content: str) -> None:
        """Append *content* to the current streaming message.

        No-op if there is no active stream (defensive; callers should
        check before calling but the TUI should not crash on misordering).
        """
        if self._streaming_idx is None:
            return
        msg = self._messages[self._streaming_idx]
        msg.content += content
        label = self._labels[self._streaming_idx]
        label.update(self._format_message(msg))
        self.scroll_end(animate=False)

    def finish_streaming(self) -> None:
        """Mark the current streaming message as complete."""
        if self._streaming_idx is None:
            return
        self._messages[self._streaming_idx].is_streaming = False
        self._streaming_idx = None

    def clear(self) -> None:
        """Remove all messages from the transcript."""
        for label in self._labels:
            label.remove()
        self._messages.clear()
        self._labels.clear()
        self._streaming_idx = None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _make_label(self, msg: TranscriptMessage) -> Label:
        """Create a Textual Label widget for *msg*."""
        return Label(
            self._format_message(msg),
            classes=_role_css_class(msg.role),
        )

    def _format_message(self, msg: TranscriptMessage) -> str:
        """Format *msg* for display.

        Prefixes role name, strips markdown, appends a streaming cursor
        while the message is still being written.
        """
        prefix_map = {
            MessageRole.USER: "You",
            MessageRole.ASSISTANT: "Arc",
            MessageRole.SYSTEM: "System",
            MessageRole.TOOL: "Tool",
            MessageRole.ERROR: "Error",
        }
        prefix = prefix_map.get(msg.role, msg.role.value.capitalize())
        body = _strip_markdown(msg.content)
        cursor = " ▋" if msg.is_streaming else ""
        return f"{prefix}: {body}{cursor}"
