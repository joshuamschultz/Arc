"""Internal message construction helpers. arcllm-compatible."""
from __future__ import annotations

from typing import Any


class Msg:
    __slots__ = ("role", "content")

    def __init__(self, role: str, content: Any) -> None:
        self.role = role
        self.content = content


class TextBlock:
    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text


class ToolUseBlock:
    __slots__ = ("id", "name", "arguments")

    def __init__(self, id: str, name: str, arguments: dict[str, Any]) -> None:
        self.id = id
        self.name = name
        self.arguments = arguments


class ToolResultBlock:
    __slots__ = ("tool_use_id", "content")

    def __init__(self, tool_use_id: str, content: str) -> None:
        self.tool_use_id = tool_use_id
        self.content = content


def user_message(text: str) -> Msg:
    return Msg(role="user", content=text)


def system_message(text: str) -> Msg:
    return Msg(role="system", content=text)


def assistant_message(content: list[Any]) -> Msg:
    return Msg(role="assistant", content=content)


def tool_result(tool_use_id: str, content: str) -> Msg:
    return Msg(role="tool", content=[ToolResultBlock(tool_use_id=tool_use_id, content=content)])
