"""Message construction helpers. Uses arcllm types directly."""
from __future__ import annotations

from typing import Any

from arcllm.types import Message, TextBlock, ToolResultBlock, ToolUseBlock

# Re-export arcllm types so existing imports work
__all__ = ["Message", "TextBlock", "ToolUseBlock", "ToolResultBlock",
           "user_message", "system_message", "assistant_message", "tool_result"]


def user_message(text: str) -> Message:
    return Message(role="user", content=text)


def system_message(text: str) -> Message:
    return Message(role="system", content=text)


def assistant_message(content: list[Any]) -> Message:
    return Message(role="assistant", content=content)


def tool_result(tool_use_id: str, content: str) -> Message:
    return Message(role="tool", content=[ToolResultBlock(tool_use_id=tool_use_id, content=content)])
