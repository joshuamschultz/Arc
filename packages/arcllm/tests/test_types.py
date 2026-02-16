"""Tests for ArcLLM core types."""

import pytest
from pydantic import ValidationError

from arcllm import (
    ArcLLMConfigError,
    ArcLLMError,
    ArcLLMParseError,
    ImageBlock,
    LLMProvider,
    LLMResponse,
    Message,
    TextBlock,
    Tool,
    ToolCall,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)


def test_message_string_content():
    msg = Message(role="user", content="Hello")
    assert msg.role == "user"
    assert msg.content == "Hello"


def test_message_contentblock_list():
    msg = Message(
        role="assistant",
        content=[
            TextBlock(text="Here's the result"),
            ToolUseBlock(
                id="call_1",
                name="search",
                arguments={"query": "test"},
            ),
        ],
    )
    assert msg.role == "assistant"
    assert len(msg.content) == 2
    assert isinstance(msg.content[0], TextBlock)
    assert isinstance(msg.content[1], ToolUseBlock)


def test_each_contentblock_variant():
    text = TextBlock(text="hello")
    assert text.type == "text"

    image = ImageBlock(source="base64data", media_type="image/png")
    assert image.type == "image"

    tool_use = ToolUseBlock(id="t1", name="calc", arguments={"x": 1})
    assert tool_use.type == "tool_use"

    tool_result = ToolResultBlock(tool_use_id="t1", content="42")
    assert tool_result.type == "tool_result"


def test_discriminated_union():
    msg = Message(
        role="assistant",
        content=[
            {"type": "text", "text": "thinking..."},
            {"type": "tool_use", "id": "c1", "name": "search", "arguments": {}},
        ],
    )
    assert isinstance(msg.content[0], TextBlock)
    assert isinstance(msg.content[1], ToolUseBlock)


def test_invalid_role_rejected():
    with pytest.raises(ValidationError):
        Message(role="invalid", content="test")


def test_toolcall_creation():
    tc = ToolCall(id="call_1", name="search", arguments={"query": "test"})
    assert tc.id == "call_1"
    assert tc.name == "search"
    assert tc.arguments == {"query": "test"}


def test_llmresponse_with_toolcalls():
    resp = LLMResponse(
        content=None,
        tool_calls=[
            ToolCall(id="c1", name="search", arguments={"q": "hello"}),
            ToolCall(id="c2", name="calc", arguments={"expr": "1+1"}),
        ],
        usage=Usage(input_tokens=100, output_tokens=50, total_tokens=150),
        model="claude-sonnet-4-20250514",
        stop_reason="tool_use",
    )
    assert resp.stop_reason == "tool_use"
    assert len(resp.tool_calls) == 2

    # Verify tool_calls defaults to empty list
    resp2 = LLMResponse(
        usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
        model="test-model",
        stop_reason="end_turn",
    )
    assert resp2.tool_calls == []


def test_llmresponse_no_content():
    resp = LLMResponse(
        content=None,
        usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
        model="test-model",
        stop_reason="tool_use",
    )
    assert resp.content is None


def test_usage_optional_fields():
    usage = Usage(input_tokens=100, output_tokens=50, total_tokens=150)
    assert usage.cache_read_tokens is None
    assert usage.cache_write_tokens is None
    assert usage.reasoning_tokens is None

    usage_full = Usage(
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        cache_read_tokens=20,
        cache_write_tokens=10,
        reasoning_tokens=30,
    )
    assert usage_full.cache_read_tokens == 20
    assert usage_full.reasoning_tokens == 30


def test_tool_definition():
    tool = Tool(
        name="search",
        description="Search the web",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
        },
    )
    assert tool.name == "search"
    assert tool.parameters["type"] == "object"


def test_toolresultblock_nested():
    result = ToolResultBlock(
        tool_use_id="c1",
        content=[
            TextBlock(text="Found 3 results"),
            TextBlock(text="Result details here"),
        ],
    )
    assert isinstance(result.content, list)
    assert len(result.content) == 2
    assert isinstance(result.content[0], TextBlock)


def test_parse_error():
    raw = '{"broken json'
    try:
        import json

        json.loads(raw)
    except json.JSONDecodeError as e:
        err = ArcLLMParseError(raw_string=raw, original_error=e)

    assert err.raw_string == raw
    assert isinstance(err.original_error, Exception)
    assert "Failed to parse" in str(err)


# --- FR-11: ArcLLMConfigError ---


def test_config_error():
    err = ArcLLMConfigError("Missing API key")
    assert str(err) == "Missing API key"
    assert isinstance(err, ArcLLMError)


# --- FR-12: LLMProvider ABC ---


def test_llmprovider_cannot_instantiate():
    with pytest.raises(TypeError):
        LLMProvider()


def test_llmprovider_concrete_subclass():
    class FakeProvider(LLMProvider):
        name = "fake"

        async def invoke(self, messages, tools=None, **kwargs):
            return LLMResponse(
                content="hello",
                usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
                model="fake-1",
                stop_reason="end_turn",
            )

        def validate_config(self):
            return True

    provider = FakeProvider()
    assert provider.name == "fake"
    assert provider.validate_config() is True


# --- SDD edge cases ---


def test_message_empty_content_list():
    msg = Message(role="user", content=[])
    assert msg.content == []


def test_llmresponse_content_and_toolcalls():
    resp = LLMResponse(
        content="Here's what I found, and I'm also calling a tool:",
        tool_calls=[ToolCall(id="c1", name="search", arguments={"q": "test"})],
        usage=Usage(input_tokens=50, output_tokens=30, total_tokens=80),
        model="test-model",
        stop_reason="tool_use",
    )
    assert resp.content is not None
    assert len(resp.tool_calls) == 1


def test_toolresultblock_nested_tooluse():
    result = ToolResultBlock(
        tool_use_id="c1",
        content=[
            ToolUseBlock(id="nested_1", name="sub_tool", arguments={"x": 1}),
        ],
    )
    assert isinstance(result.content[0], ToolUseBlock)


def test_usage_zero_tokens():
    usage = Usage(input_tokens=0, output_tokens=0, total_tokens=0)
    assert usage.input_tokens == 0
    assert usage.total_tokens == 0


# --- StopReason normalization ---


def test_stop_reason_valid_values():
    """All five canonical stop reasons are accepted by LLMResponse."""
    usage = Usage(input_tokens=10, output_tokens=5, total_tokens=15)
    for reason in ["end_turn", "tool_use", "max_tokens", "stop_sequence", "content_filter"]:
        resp = LLMResponse(
            usage=usage,
            model="test-model",
            stop_reason=reason,
        )
        assert resp.stop_reason == reason


def test_stop_reason_invalid_value():
    """Invalid stop reason strings are rejected by pydantic validation."""
    usage = Usage(input_tokens=10, output_tokens=5, total_tokens=15)
    with pytest.raises(ValidationError):
        LLMResponse(
            usage=usage,
            model="test-model",
            stop_reason="stop",  # OpenAI-native value, should be rejected
        )
