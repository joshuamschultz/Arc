"""Message construction helpers. Uses arcllm types directly.

Re-exports ``TextBlock`` and ``ToolUseBlock`` so callers in arcrun
(e.g. strategies/react.py) can import block types from a single place.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import arcllm

ContentBlock = arcllm.ContentBlock
Message = arcllm.Message
TextBlock = arcllm.TextBlock
ToolResultBlock = arcllm.ToolResultBlock
ToolUseBlock = arcllm.ToolUseBlock

__all__ = [
    "ContentBlock",
    "Message",
    "SystemPrompt",
    "TextBlock",
    "ToolResultBlock",
    "ToolUseBlock",
    "assistant_message",
    "content_text",
    "system_message",
    "system_messages",
    "tool_result",
    "user_message",
]

# A system prompt is either one string or an ordered list of segments. A caller
# that knows which parts of its prompt change at different rates passes the
# segments most-stable-first. That order is a fact about the prompt, not a
# provider directive: what a provider does with it — if anything — is the
# adapter's business, and arcrun states nothing about any provider's mechanism.
SystemPrompt = str | Sequence[str]


def user_message(text: str | list[arcllm.ContentBlock]) -> Message:
    """A user turn: plain words, or blocks when it carries more than words."""
    return Message(role="user", content=text)


def content_text(content: Any) -> str:
    """The words in a message's content, whether it is text or blocks.

    Every consumer that wants a message as a *string* — an audit line, an
    event preview, a summarization prompt, a search index — goes through this.
    Reading ``.content`` directly yields a *list* for a message that carried
    an image, and that list then flows into places that expect words: it
    stringifies as a repr, or it raises where a str was assumed. One of those
    raised inside the audit chain and killed the turn it was recording.

    Blocks arrive as objects on the wire and as plain dicts from a session
    log, so both are read here; a block with no words contributes none.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    return "\n".join(part for part in (_block_words(block) for block in content) if part)


def _block_words(block: Any) -> str:
    """One block as words, naming its kind when it has none.

    A wordless block — an image, a tool call — must not read as *nothing*: a
    projection of a photo-only turn that comes back empty tells an audit line,
    a log and a summarizer alike that an empty message arrived.
    """
    read = (
        block.get
        if isinstance(block, dict)
        else lambda key, default=None: getattr(block, key, default)
    )
    text = read("text", "")
    if text:
        return str(text)
    kind = read("type", "")
    return f"[{kind}]" if kind else ""


def system_message(text: str) -> Message:
    return Message(role="system", content=text)


def system_messages(prompt: SystemPrompt) -> list[Message]:
    """One system message per segment, dropping empties."""
    parts = [prompt] if isinstance(prompt, str) else list(prompt)
    return [system_message(p) for p in parts if p]


def assistant_message(content: list[Any]) -> Message:
    return Message(role="assistant", content=content)


def tool_result(tool_use_id: str, content: str) -> Message:
    return Message(role="tool", content=[ToolResultBlock(tool_use_id=tool_use_id, content=content)])
