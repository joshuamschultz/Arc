"""Message construction helpers. Uses arcllm types directly.

Re-exports ``TextBlock`` and ``ToolUseBlock`` so callers in arcrun
(e.g. strategies/react.py) can import block types from a single place.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from arcllm.types import Message, TextBlock, ToolResultBlock, ToolUseBlock

__all__ = [
    "Message",
    "SystemPrompt",
    "TextBlock",
    "ToolResultBlock",
    "ToolUseBlock",
    "assistant_message",
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


def user_message(text: str) -> Message:
    return Message(role="user", content=text)


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
