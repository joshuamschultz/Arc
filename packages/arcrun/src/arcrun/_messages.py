"""Message construction helpers. Uses arcllm types directly.

Re-exports ``TextBlock`` and ``ToolUseBlock`` so callers in arcrun
(e.g. strategies/react.py) can import block types from a single place.
"""

from __future__ import annotations

from typing import Any

from arcllm.types import Message, TextBlock, ToolResultBlock, ToolUseBlock

__all__ = [
    "Message",
    "TextBlock",
    "ToolResultBlock",
    "ToolUseBlock",
    "assistant_message",
    "system_message",
    "tool_result",
    "user_message",
]


def user_message(text: str) -> Message:
    return Message(role="user", content=text)


def system_message(text: str) -> Message:
    return Message(role="system", content=text)


def assistant_message(content: list[Any]) -> Message:
    return Message(role="assistant", content=content)


def tool_result(tool_use_id: str, content: str) -> Message:
    return Message(role="tool", content=[ToolResultBlock(tool_use_id=tool_use_id, content=content)])
