"""Tests for the arcllm→browser-use LLM adapter (import-safe surface).

The message conversion and the structured-output mechanic are the parts
that must be correct regardless of browser-use being installed — they are
exercised here with fake arcllm providers and a real target schema. The
``ChatInvokeCompletion`` wrapping needs the browser-use package and is
only exercised in a live smoke test.
"""

from __future__ import annotations

from typing import Any

import pytest
from arcllm.types import ImageBlock, Message, TextBlock
from pydantic import BaseModel

from arcagent.modules.browser._browser_use.adapter import (
    ArcLLMChatModel,
    _arc_content,
    _to_arc_messages,
)


class _Part:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _Msg:
    def __init__(self, role: str, content: Any) -> None:
        self.role = role
        self.content = content


class _Call:
    def __init__(self, name: str, arguments: dict[str, Any]) -> None:
        self.name = name
        self.arguments = arguments


class _Resp:
    def __init__(self, content: str | None = None, tool_calls: list[_Call] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.usage = None
        self.stop_reason = "end_turn"


class _FakeProvider:
    def __init__(self, response: _Resp) -> None:
        self._response = response
        self.model = "fake-model"
        self.calls: list[Any] = []

    async def invoke(self, messages: list[Message], tools: Any = None, **_kw: Any) -> _Resp:
        self.calls.append((messages, tools))
        return self._response


class _Out(BaseModel):
    action: str
    value: int


class TestContentConversion:
    def test_plain_string_passthrough(self) -> None:
        assert _arc_content("hello") == "hello"

    def test_empty_list_is_empty_string(self) -> None:
        assert _arc_content([]) == ""

    def test_text_and_image_parts_map_to_blocks(self) -> None:
        parts = [
            _Part(type="text", text="look:"),
            _Part(type="image_url", image_url=_Part(url="data:image/png;base64,AAA", media_type="image/png")),
        ]
        blocks = _arc_content(parts)
        assert isinstance(blocks, list)
        assert isinstance(blocks[0], TextBlock) and blocks[0].text == "look:"
        assert isinstance(blocks[1], ImageBlock)
        assert blocks[1].source == "data:image/png;base64,AAA"
        assert blocks[1].media_type == "image/png"

    def test_messages_map_role_and_content(self) -> None:
        msgs = _to_arc_messages([_Msg("system", "sys"), _Msg("user", "hi")])
        assert [m.role for m in msgs] == ["system", "user"]
        assert msgs[1].content == "hi"


@pytest.mark.asyncio
class TestStructuredOutput:
    async def test_tool_call_is_validated_into_schema(self) -> None:
        provider = _FakeProvider(
            _Resp(tool_calls=[_Call("emit_output", {"action": "click", "value": 3})])
        )
        model = ArcLLMChatModel(provider, model="m")
        out = await model._invoke_structured([Message(role="user", content="x")], _Out)
        assert isinstance(out, _Out)
        assert out.action == "click" and out.value == 3
        # A single output tool was offered to the model.
        _msgs, tools = provider.calls[0]
        assert tools[0].name == "emit_output"

    async def test_falls_back_to_json_content(self) -> None:
        provider = _FakeProvider(_Resp(content='{"action": "type", "value": 5}'))
        model = ArcLLMChatModel(provider, model="m")
        out = await model._invoke_structured([Message(role="user", content="x")], _Out)
        assert out.value == 5

    async def test_no_structured_output_raises(self) -> None:
        provider = _FakeProvider(_Resp())  # no tool call, no content
        model = ArcLLMChatModel(provider, model="m")
        with pytest.raises(ValueError, match="no structured output"):
            await model._invoke_structured([Message(role="user", content="x")], _Out)
