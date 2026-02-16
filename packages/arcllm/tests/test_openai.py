"""Tests for ArcLLM OpenAI adapter."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from arcllm.config import (
    ModelMetadata,
    ProviderConfig,
    ProviderSettings,
)
from arcllm.exceptions import (
    ArcLLMAPIError,
    ArcLLMParseError,
)
from arcllm.types import (
    LLMResponse,
    Message,
    TextBlock,
    Tool,
    ToolCall,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FAKE_MODEL = "gpt-4o-test"

FAKE_PROVIDER_SETTINGS = ProviderSettings(
    api_format="openai-chat",
    base_url="https://api.openai.com",
    api_key_env="ARCLLM_TEST_KEY",
    default_model=FAKE_MODEL,
    default_temperature=0.7,
)

FAKE_MODEL_META = ModelMetadata(
    context_window=128000,
    max_output_tokens=16384,
    supports_tools=True,
    supports_vision=True,
    supports_thinking=False,
    input_modalities=["text", "image"],
    cost_input_per_1m=2.50,
    cost_output_per_1m=10.00,
    cost_cache_read_per_1m=1.25,
    cost_cache_write_per_1m=2.50,
)

FAKE_CONFIG = ProviderConfig(
    provider=FAKE_PROVIDER_SETTINGS,
    models={FAKE_MODEL: FAKE_MODEL_META},
)


@pytest.fixture(autouse=True)
def _set_test_api_key(monkeypatch):
    """Ensure the test API key env var is set for all tests."""
    monkeypatch.setenv("ARCLLM_TEST_KEY", "test-openai-key-456")


def _make_openai_text_response(
    text: str = "Hello!",
    model: str = FAKE_MODEL,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    finish_reason: str = "stop",
) -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": text,
                },
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _make_openai_tool_response(
    tool_id: str = "call_01",
    tool_name: str = "search",
    tool_args: dict | None = None,
    text: str | None = None,
) -> dict:
    tool_calls = [
        {
            "id": tool_id,
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": json.dumps(tool_args or {"query": "test"}),
            },
        }
    ]
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": FAKE_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": text,
                    "tool_calls": tool_calls,
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 15,
            "total_tokens": 35,
        },
    }


# ---------------------------------------------------------------------------
# TestOpenAIHeaders
# ---------------------------------------------------------------------------


class TestOpenAIHeaders:
    def test_openai_bearer_auth(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        headers = adapter._build_headers()
        assert headers["Authorization"] == "Bearer test-openai-key-456"

    def test_openai_content_type(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        headers = adapter._build_headers()
        assert headers["Content-Type"] == "application/json"

    def test_openai_name(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        assert adapter.name == "openai"


# ---------------------------------------------------------------------------
# TestOpenAIRequestBuilding
# ---------------------------------------------------------------------------


class TestOpenAIRequestBuilding:
    def test_simple_text_request(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        messages = [Message(role="user", content="Hello")]
        body = adapter._build_request_body(messages)
        assert body["model"] == FAKE_MODEL
        assert body["messages"] == [{"role": "user", "content": "Hello"}]

    def test_system_message_inline(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        messages = [
            Message(role="system", content="You are helpful."),
            Message(role="user", content="Hi"),
        ]
        body = adapter._build_request_body(messages)
        # System messages stay in-line (not extracted like Anthropic)
        assert len(body["messages"]) == 2
        assert body["messages"][0]["role"] == "system"
        assert body["messages"][0]["content"] == "You are helpful."
        assert body["messages"][1]["role"] == "user"

    def test_tool_formatting(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        tools = [
            Tool(
                name="search",
                description="Search the web",
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            )
        ]
        messages = [Message(role="user", content="Search for cats")]
        body = adapter._build_request_body(messages, tools=tools)
        assert len(body["tools"]) == 1
        tool = body["tools"][0]
        # OpenAI wraps in {"type": "function", "function": {...}}
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "search"
        assert tool["function"]["description"] == "Search the web"
        assert "parameters" in tool["function"]
        # Our `parameters` key stays as `parameters` in OpenAI (not `input_schema`)
        assert "input_schema" not in tool["function"]

    def test_tool_result_flattening(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        messages = [
            Message(role="user", content="Search for cats"),
            Message(
                role="tool",
                content=[
                    ToolResultBlock(tool_use_id="t1", content="Found 3 cats"),
                    ToolResultBlock(tool_use_id="t2", content="Also found dogs"),
                ],
            ),
        ]
        body = adapter._build_request_body(messages)
        # One ArcLLM message with 2 ToolResultBlocks -> 3 total OpenAI messages
        assert len(body["messages"]) == 3
        assert body["messages"][1]["role"] == "tool"
        assert body["messages"][1]["tool_call_id"] == "t1"
        assert body["messages"][1]["content"] == "Found 3 cats"
        assert body["messages"][2]["role"] == "tool"
        assert body["messages"][2]["tool_call_id"] == "t2"
        assert body["messages"][2]["content"] == "Also found dogs"

    def test_tool_result_single(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        messages = [
            Message(
                role="tool",
                content=[
                    ToolResultBlock(tool_use_id="t1", content="42"),
                ],
            ),
        ]
        body = adapter._build_request_body(messages)
        assert len(body["messages"]) == 1
        assert body["messages"][0]["role"] == "tool"
        assert body["messages"][0]["tool_call_id"] == "t1"
        assert body["messages"][0]["content"] == "42"

    def test_assistant_tool_use_formatting(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        messages = [
            Message(
                role="assistant",
                content=[
                    ToolUseBlock(id="t1", name="calc", arguments={"x": 1}),
                ],
            ),
        ]
        body = adapter._build_request_body(messages)
        msg = body["messages"][0]
        assert msg["role"] == "assistant"
        assert msg["content"] is None
        assert len(msg["tool_calls"]) == 1
        tc = msg["tool_calls"][0]
        assert tc["id"] == "t1"
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "calc"
        assert tc["function"]["arguments"] == '{"x": 1}'

    def test_kwargs_override_defaults(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        messages = [Message(role="user", content="Hi")]
        body = adapter._build_request_body(messages, max_tokens=1000, temperature=0.2)
        assert body["max_tokens"] == 1000
        assert body["temperature"] == 0.2


# ---------------------------------------------------------------------------
# TestOpenAIResponseParsing
# ---------------------------------------------------------------------------


class TestOpenAIResponseParsing:
    def test_text_response(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        data = _make_openai_text_response(text="Hello world")
        resp = adapter._parse_response(data)
        assert isinstance(resp, LLMResponse)
        assert resp.content == "Hello world"
        assert resp.tool_calls == []
        assert resp.stop_reason == "end_turn"

    def test_tool_use_response(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        data = _make_openai_tool_response(
            tool_id="call_1", tool_name="search", tool_args={"query": "cats"}
        )
        resp = adapter._parse_response(data)
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].id == "call_1"
        assert resp.tool_calls[0].name == "search"
        assert resp.tool_calls[0].arguments == {"query": "cats"}
        assert resp.stop_reason == "tool_use"

    def test_mixed_response(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        data = _make_openai_tool_response(
            text="Let me search for that.",
            tool_id="call_1",
            tool_name="search",
            tool_args={"query": "cats"},
        )
        resp = adapter._parse_response(data)
        assert resp.content == "Let me search for that."
        assert len(resp.tool_calls) == 1

    def test_null_content_response(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        data = _make_openai_tool_response()
        # Ensure content is None in the raw response
        data["choices"][0]["message"]["content"] = None
        resp = adapter._parse_response(data)
        assert resp.content is None
        assert len(resp.tool_calls) == 1

    def test_usage_parsing(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        data = _make_openai_text_response(prompt_tokens=100, completion_tokens=50)
        resp = adapter._parse_response(data)
        assert resp.usage.input_tokens == 100
        assert resp.usage.output_tokens == 50
        assert resp.usage.total_tokens == 150

    def test_usage_reasoning_tokens(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        data = _make_openai_text_response()
        data["usage"]["completion_tokens_details"] = {"reasoning_tokens": 25}
        resp = adapter._parse_response(data)
        assert resp.usage.reasoning_tokens == 25

    def test_raw_response_stored(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        data = _make_openai_text_response()
        resp = adapter._parse_response(data)
        assert resp.raw is data


# ---------------------------------------------------------------------------
# TestOpenAIStopReasonMapping
# ---------------------------------------------------------------------------


class TestOpenAIStopReasonMapping:
    def test_stop_maps_to_end_turn(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        data = _make_openai_text_response(finish_reason="stop")
        resp = adapter._parse_response(data)
        assert resp.stop_reason == "end_turn"

    def test_tool_calls_maps_to_tool_use(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        data = _make_openai_tool_response()
        resp = adapter._parse_response(data)
        assert resp.stop_reason == "tool_use"

    def test_length_maps_to_max_tokens(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        data = _make_openai_text_response(finish_reason="length")
        resp = adapter._parse_response(data)
        assert resp.stop_reason == "max_tokens"

    def test_content_filter_maps_to_content_filter(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        data = _make_openai_text_response(finish_reason="content_filter")
        resp = adapter._parse_response(data)
        assert resp.stop_reason == "content_filter"

    def test_unknown_finish_reason_defaults_to_end_turn(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        data = _make_openai_text_response(finish_reason="some_future_reason")
        resp = adapter._parse_response(data)
        assert resp.stop_reason == "end_turn"


# ---------------------------------------------------------------------------
# TestOpenAIToolCallParsing
# ---------------------------------------------------------------------------


class TestOpenAIToolCallParsing:
    def test_tool_call_json_string_arguments(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        tc = {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "calc",
                "arguments": '{"x": 1, "y": 2}',
            },
        }
        result = adapter._parse_tool_call(tc)
        assert result.arguments == {"x": 1, "y": 2}

    def test_tool_call_dict_arguments(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        tc = {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "calc",
                "arguments": {"x": 1},
            },
        }
        result = adapter._parse_tool_call(tc)
        assert result.arguments == {"x": 1}

    def test_tool_call_bad_arguments(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        tc = {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "calc",
                "arguments": "not valid json {{{",
            },
        }
        with pytest.raises(ArcLLMParseError):
            adapter._parse_tool_call(tc)

    def test_tool_call_unexpected_argument_type(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        tc = {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "calc",
                "arguments": 12345,
            },
        }
        with pytest.raises(ArcLLMParseError):
            adapter._parse_tool_call(tc)


# ---------------------------------------------------------------------------
# TestOpenAIErrorHandling
# ---------------------------------------------------------------------------


class TestOpenAIErrorHandling:
    @pytest.mark.asyncio
    async def test_http_429_error(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        mock_response = httpx.Response(
            429,
            text="rate limited",
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        )
        adapter._client = AsyncMock()
        adapter._client.post = AsyncMock(return_value=mock_response)

        with pytest.raises(ArcLLMAPIError) as exc_info:
            await adapter.invoke([Message(role="user", content="Hi")])
        assert exc_info.value.status_code == 429
        assert exc_info.value.provider == "openai"

    @pytest.mark.asyncio
    async def test_http_401_error(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        mock_response = httpx.Response(
            401,
            text="unauthorized",
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        )
        adapter._client = AsyncMock()
        adapter._client.post = AsyncMock(return_value=mock_response)

        with pytest.raises(ArcLLMAPIError) as exc_info:
            await adapter.invoke([Message(role="user", content="Hi")])
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_http_500_error(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        mock_response = httpx.Response(
            500,
            text="internal server error",
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        )
        adapter._client = AsyncMock()
        adapter._client.post = AsyncMock(return_value=mock_response)

        with pytest.raises(ArcLLMAPIError) as exc_info:
            await adapter.invoke([Message(role="user", content="Hi")])
        assert exc_info.value.status_code == 500


# ---------------------------------------------------------------------------
# TestOpenAIFullCycle
# ---------------------------------------------------------------------------


class TestOpenAIFullCycle:
    @pytest.mark.asyncio
    async def test_complete_text_cycle(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        response_data = _make_openai_text_response(text="Hello!")
        mock_response = httpx.Response(
            200,
            json=response_data,
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        )
        adapter._client = AsyncMock()
        adapter._client.post = AsyncMock(return_value=mock_response)

        resp = await adapter.invoke([Message(role="user", content="Hi")])
        assert isinstance(resp, LLMResponse)
        assert resp.content == "Hello!"
        assert resp.stop_reason == "end_turn"
        assert resp.usage.total_tokens == 15

    @pytest.mark.asyncio
    async def test_complete_tool_cycle(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        response_data = _make_openai_tool_response(
            tool_id="call_1", tool_name="search", tool_args={"query": "cats"}
        )
        mock_response = httpx.Response(
            200,
            json=response_data,
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        )
        adapter._client = AsyncMock()
        adapter._client.post = AsyncMock(return_value=mock_response)

        tools = [
            Tool(
                name="search",
                description="Search the web",
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            )
        ]
        resp = await adapter.invoke(
            [Message(role="user", content="Search cats")], tools=tools
        )
        assert isinstance(resp, LLMResponse)
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "search"
        assert resp.stop_reason == "tool_use"


# ---------------------------------------------------------------------------
# TestOpenAIEdgeCases
# ---------------------------------------------------------------------------


class TestOpenAIEdgeCases:
    def test_multiple_tool_calls_in_response(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        data = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": FAKE_MODEL,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "search",
                                    "arguments": '{"query": "cats"}',
                                },
                            },
                            {
                                "id": "call_2",
                                "type": "function",
                                "function": {
                                    "name": "calc",
                                    "arguments": '{"expr": "1+1"}',
                                },
                            },
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {
                "prompt_tokens": 30,
                "completion_tokens": 20,
                "total_tokens": 50,
            },
        }
        resp = adapter._parse_response(data)
        assert len(resp.tool_calls) == 2
        assert resp.tool_calls[0].name == "search"
        assert resp.tool_calls[1].name == "calc"
        assert resp.stop_reason == "tool_use"

    def test_list_content_block_tool_result(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        messages = [
            Message(
                role="tool",
                content=[
                    ToolResultBlock(
                        tool_use_id="t1",
                        content=[TextBlock(text="Result A"), TextBlock(text="Result B")],
                    ),
                ],
            ),
        ]
        body = adapter._build_request_body(messages)
        assert len(body["messages"]) == 1
        assert body["messages"][0]["content"] == "Result A Result B"

    def test_mixed_assistant_content_formatting(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        messages = [
            Message(
                role="assistant",
                content=[
                    TextBlock(text="Let me search."),
                    ToolUseBlock(id="t1", name="search", arguments={"q": "cats"}),
                    ToolUseBlock(id="t2", name="calc", arguments={"x": 1}),
                ],
            ),
        ]
        body = adapter._build_request_body(messages)
        msg = body["messages"][0]
        assert msg["role"] == "assistant"
        assert msg["content"] == "Let me search."
        assert len(msg["tool_calls"]) == 2


# ---------------------------------------------------------------------------
# TestOpenAIImageBlock
# ---------------------------------------------------------------------------


class TestOpenAIImageBlock:
    def test_image_block_formatted_as_image_url(self):
        from arcllm.adapters.openai import OpenaiAdapter
        from arcllm.types import ImageBlock

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        messages = [
            Message(
                role="user",
                content=[
                    ImageBlock(source="base64data", media_type="image/png"),
                ],
            ),
        ]
        body = adapter._build_request_body(messages)
        msg = body["messages"][0]
        assert msg["role"] == "user"
        assert isinstance(msg["content"], list)
        assert len(msg["content"]) == 1
        assert msg["content"][0]["type"] == "image_url"
        assert msg["content"][0]["image_url"]["url"] == "data:image/png;base64,base64data"

    def test_mixed_text_and_image_blocks(self):
        from arcllm.adapters.openai import OpenaiAdapter
        from arcllm.types import ImageBlock

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        messages = [
            Message(
                role="user",
                content=[
                    TextBlock(text="What is in this image?"),
                    ImageBlock(source="base64data", media_type="image/jpeg"),
                ],
            ),
        ]
        body = adapter._build_request_body(messages)
        msg = body["messages"][0]
        assert isinstance(msg["content"], list)
        assert len(msg["content"]) == 2
        assert msg["content"][0] == {"type": "text", "text": "What is in this image?"}
        assert msg["content"][1]["type"] == "image_url"


# ---------------------------------------------------------------------------
# TestOpenAIRetryAfter
# ---------------------------------------------------------------------------


class TestOpenAIRetryAfter:
    @pytest.mark.asyncio
    async def test_retry_after_header_passed_to_error(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        mock_response = httpx.Response(
            429,
            text="rate limited",
            headers={"retry-after": "5"},
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        )
        adapter._client = AsyncMock()
        adapter._client.post = AsyncMock(return_value=mock_response)

        with pytest.raises(ArcLLMAPIError) as exc_info:
            await adapter.invoke([Message(role="user", content="Hi")])
        assert exc_info.value.retry_after == 5.0

    @pytest.mark.asyncio
    async def test_no_retry_after_header(self):
        from arcllm.adapters.openai import OpenaiAdapter

        adapter = OpenaiAdapter(FAKE_CONFIG, FAKE_MODEL)
        mock_response = httpx.Response(
            500,
            text="server error",
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        )
        adapter._client = AsyncMock()
        adapter._client.post = AsyncMock(return_value=mock_response)

        with pytest.raises(ArcLLMAPIError) as exc_info:
            await adapter.invoke([Message(role="user", content="Hi")])
        assert exc_info.value.retry_after is None
