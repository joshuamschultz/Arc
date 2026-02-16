"""Tests for ArcLLM Anthropic adapter."""

import json
import os
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from arcllm.adapters.base import BaseAdapter
from arcllm.config import (
    ModelMetadata,
    ProviderConfig,
    ProviderSettings,
)
from arcllm.exceptions import (
    ArcLLMAPIError,
    ArcLLMConfigError,
    ArcLLMError,
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

FAKE_MODEL = "claude-test-1"

FAKE_PROVIDER_SETTINGS = ProviderSettings(
    api_format="anthropic-messages",
    base_url="https://api.anthropic.com",
    api_key_env="ARCLLM_TEST_KEY",
    default_model=FAKE_MODEL,
    default_temperature=0.7,
)

FAKE_MODEL_META = ModelMetadata(
    context_window=200000,
    max_output_tokens=8192,
    supports_tools=True,
    supports_vision=True,
    supports_thinking=True,
    input_modalities=["text", "image"],
    cost_input_per_1m=3.0,
    cost_output_per_1m=15.0,
    cost_cache_read_per_1m=0.3,
    cost_cache_write_per_1m=3.75,
)

FAKE_CONFIG = ProviderConfig(
    provider=FAKE_PROVIDER_SETTINGS,
    models={FAKE_MODEL: FAKE_MODEL_META},
)


@pytest.fixture(autouse=True)
def _set_test_api_key(monkeypatch):
    """Ensure the test API key env var is set for all tests."""
    monkeypatch.setenv("ARCLLM_TEST_KEY", "test-ant-key-123")


def _make_anthropic_text_response(
    text: str = "Hello!",
    model: str = FAKE_MODEL,
    input_tokens: int = 10,
    output_tokens: int = 5,
    stop_reason: str = "end_turn",
) -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


def _make_anthropic_tool_response(
    tool_id: str = "toolu_01",
    tool_name: str = "search",
    tool_input: dict | None = None,
    text: str | None = None,
) -> dict:
    content = []
    if text:
        content.append({"type": "text", "text": text})
    content.append({
        "type": "tool_use",
        "id": tool_id,
        "name": tool_name,
        "input": tool_input or {"query": "test"},
    })
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": FAKE_MODEL,
        "content": content,
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 20, "output_tokens": 15},
    }


# ---------------------------------------------------------------------------
# T3.1: ArcLLMAPIError
# ---------------------------------------------------------------------------


class TestArcLLMAPIError:
    def test_api_error_attributes(self):
        err = ArcLLMAPIError(status_code=429, body="rate limited", provider="anthropic")
        assert err.status_code == 429
        assert err.body == "rate limited"
        assert err.provider == "anthropic"

    def test_api_error_inherits_arcllm_error(self):
        err = ArcLLMAPIError(status_code=500, body="internal", provider="anthropic")
        assert isinstance(err, ArcLLMError)
        assert isinstance(err, Exception)

    def test_api_error_message_format(self):
        err = ArcLLMAPIError(status_code=401, body="invalid key", provider="anthropic")
        msg = str(err)
        assert "anthropic" in msg
        assert "401" in msg
        assert "invalid key" in msg

    def test_api_error_body_truncation(self):
        long_body = "x" * 1000
        err = ArcLLMAPIError(status_code=500, body=long_body, provider="anthropic")
        # Full body available on attribute
        assert err.body == long_body
        assert len(err.body) == 1000
        # __str__ truncates for log safety
        msg = str(err)
        assert len(msg) < 600  # truncated at 500 + prefix
        assert msg.endswith("...")


# ---------------------------------------------------------------------------
# T3.2: BaseAdapter
# ---------------------------------------------------------------------------


class _ConcreteAdapter(BaseAdapter):
    """Minimal concrete subclass for testing BaseAdapter plumbing."""

    async def invoke(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs,
    ) -> LLMResponse:
        raise NotImplementedError("stub")


class TestBaseAdapter:
    def test_base_adapter_stores_config(self):
        adapter = _ConcreteAdapter(FAKE_CONFIG, FAKE_MODEL)
        assert adapter._config is FAKE_CONFIG
        assert adapter.model_name == FAKE_MODEL

    def test_base_adapter_resolves_api_key(self):
        adapter = _ConcreteAdapter(FAKE_CONFIG, FAKE_MODEL)
        assert adapter._api_key == "test-ant-key-123"

    def test_base_adapter_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("ARCLLM_TEST_KEY", raising=False)
        with pytest.raises(ArcLLMConfigError, match="Missing environment variable"):
            _ConcreteAdapter(FAKE_CONFIG, FAKE_MODEL)

    def test_base_adapter_empty_api_key_raises(self, monkeypatch):
        monkeypatch.setenv("ARCLLM_TEST_KEY", "")
        with pytest.raises(ArcLLMConfigError, match="Missing environment variable"):
            _ConcreteAdapter(FAKE_CONFIG, FAKE_MODEL)

    @pytest.mark.asyncio
    async def test_base_adapter_context_manager(self):
        async with _ConcreteAdapter(FAKE_CONFIG, FAKE_MODEL) as adapter:
            assert adapter._client is not None
        assert adapter._client is None

    def test_base_adapter_model_meta_found(self):
        adapter = _ConcreteAdapter(FAKE_CONFIG, FAKE_MODEL)
        assert adapter._model_meta is not None
        assert adapter._model_meta.context_window == 200000

    def test_base_adapter_model_meta_not_found(self):
        adapter = _ConcreteAdapter(FAKE_CONFIG, "nonexistent-model")
        assert adapter._model_meta is None

    def test_base_adapter_validate_config(self):
        adapter = _ConcreteAdapter(FAKE_CONFIG, FAKE_MODEL)
        assert adapter.validate_config() is True

    @pytest.mark.asyncio
    async def test_base_adapter_close_idempotent(self):
        adapter = _ConcreteAdapter(FAKE_CONFIG, FAKE_MODEL)
        await adapter.close()
        await adapter.close()  # should not raise


# ---------------------------------------------------------------------------
# T3.3 + T3.4: AnthropicAdapter
# ---------------------------------------------------------------------------


class TestAnthropicHeaders:
    def test_anthropic_headers(self):
        from arcllm.adapters.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(FAKE_CONFIG, FAKE_MODEL)
        headers = adapter._build_headers()
        assert headers["x-api-key"] == "test-ant-key-123"
        assert headers["anthropic-version"] == "2023-06-01"
        assert headers["content-type"] == "application/json"

    def test_anthropic_name(self):
        from arcllm.adapters.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(FAKE_CONFIG, FAKE_MODEL)
        assert adapter.name == "anthropic"


class TestAnthropicRequestBuilding:
    def test_simple_text_request(self):
        from arcllm.adapters.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(FAKE_CONFIG, FAKE_MODEL)
        messages = [Message(role="user", content="Hello")]
        body = adapter._build_request_body(messages)
        assert body["model"] == FAKE_MODEL
        assert body["messages"] == [{"role": "user", "content": "Hello"}]
        assert "system" not in body

    def test_system_message_extraction(self):
        from arcllm.adapters.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(FAKE_CONFIG, FAKE_MODEL)
        messages = [
            Message(role="system", content="You are helpful."),
            Message(role="user", content="Hi"),
        ]
        body = adapter._build_request_body(messages)
        assert body["system"] == "You are helpful."
        assert len(body["messages"]) == 1
        assert body["messages"][0]["role"] == "user"

    def test_multiple_system_messages(self):
        from arcllm.adapters.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(FAKE_CONFIG, FAKE_MODEL)
        messages = [
            Message(role="system", content="Be concise."),
            Message(role="system", content="Use tools when needed."),
            Message(role="user", content="Hi"),
        ]
        body = adapter._build_request_body(messages)
        assert body["system"] == "Be concise.\nUse tools when needed."

    def test_tool_formatting(self):
        from arcllm.adapters.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(FAKE_CONFIG, FAKE_MODEL)
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
        assert tool["name"] == "search"
        assert tool["description"] == "Search the web"
        assert "input_schema" in tool
        assert "parameters" not in tool

    def test_tool_role_translation(self):
        from arcllm.adapters.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(FAKE_CONFIG, FAKE_MODEL)
        messages = [
            Message(role="user", content="Search for cats"),
            Message(
                role="assistant",
                content=[
                    ToolUseBlock(id="t1", name="search", arguments={"query": "cats"}),
                ],
            ),
            Message(
                role="tool",
                content=[
                    ToolResultBlock(tool_use_id="t1", content="Found 3 results"),
                ],
            ),
        ]
        body = adapter._build_request_body(messages)
        # tool role should become user
        assert body["messages"][2]["role"] == "user"

    def test_content_block_formatting(self):
        from arcllm.adapters.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(FAKE_CONFIG, FAKE_MODEL)

        # TextBlock
        tb = adapter._format_content_block(TextBlock(text="hello"))
        assert tb == {"type": "text", "text": "hello"}

        # ToolUseBlock
        tub = adapter._format_content_block(
            ToolUseBlock(id="t1", name="calc", arguments={"x": 1})
        )
        assert tub == {"type": "tool_use", "id": "t1", "name": "calc", "input": {"x": 1}}

        # ToolResultBlock with string content
        trb = adapter._format_content_block(
            ToolResultBlock(tool_use_id="t1", content="42")
        )
        assert trb == {"type": "tool_result", "tool_use_id": "t1", "content": "42"}

        # ToolResultBlock with list content
        trb2 = adapter._format_content_block(
            ToolResultBlock(
                tool_use_id="t1",
                content=[TextBlock(text="result")],
            )
        )
        assert trb2["type"] == "tool_result"
        assert isinstance(trb2["content"], list)
        assert trb2["content"][0] == {"type": "text", "text": "result"}

    def test_image_block_formatting(self):
        from arcllm.adapters.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(FAKE_CONFIG, FAKE_MODEL)
        from arcllm.types import ImageBlock

        ib = adapter._format_content_block(
            ImageBlock(source="base64data", media_type="image/png")
        )
        assert ib == {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "base64data",
            },
        }

    def test_kwargs_override_defaults(self):
        from arcllm.adapters.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(FAKE_CONFIG, FAKE_MODEL)
        messages = [Message(role="user", content="Hi")]
        body = adapter._build_request_body(messages, max_tokens=1000, temperature=0.2)
        assert body["max_tokens"] == 1000
        assert body["temperature"] == 0.2


class TestAnthropicResponseParsing:
    def test_text_response(self):
        from arcllm.adapters.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(FAKE_CONFIG, FAKE_MODEL)
        data = _make_anthropic_text_response(text="Hello world")
        resp = adapter._parse_response(data)
        assert isinstance(resp, LLMResponse)
        assert resp.content == "Hello world"
        assert resp.tool_calls == []
        assert resp.stop_reason == "end_turn"

    def test_tool_use_response(self):
        from arcllm.adapters.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(FAKE_CONFIG, FAKE_MODEL)
        data = _make_anthropic_tool_response(
            tool_id="t1", tool_name="search", tool_input={"query": "cats"}
        )
        resp = adapter._parse_response(data)
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].id == "t1"
        assert resp.tool_calls[0].name == "search"
        assert resp.tool_calls[0].arguments == {"query": "cats"}
        assert resp.stop_reason == "tool_use"

    def test_mixed_response(self):
        from arcllm.adapters.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(FAKE_CONFIG, FAKE_MODEL)
        data = _make_anthropic_tool_response(
            text="Let me search for that.",
            tool_id="t1",
            tool_name="search",
            tool_input={"query": "cats"},
        )
        resp = adapter._parse_response(data)
        assert resp.content == "Let me search for that."
        assert len(resp.tool_calls) == 1

    def test_usage_parsing(self):
        from arcllm.adapters.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(FAKE_CONFIG, FAKE_MODEL)
        data = _make_anthropic_text_response(input_tokens=100, output_tokens=50)
        resp = adapter._parse_response(data)
        assert resp.usage.input_tokens == 100
        assert resp.usage.output_tokens == 50
        assert resp.usage.total_tokens == 150

    def test_usage_cache_tokens(self):
        from arcllm.adapters.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(FAKE_CONFIG, FAKE_MODEL)
        data = _make_anthropic_text_response()
        data["usage"]["cache_read_input_tokens"] = 20
        data["usage"]["cache_creation_input_tokens"] = 10
        resp = adapter._parse_response(data)
        assert resp.usage.cache_read_tokens == 20
        assert resp.usage.cache_write_tokens == 10

    def test_stop_reason_mapping(self):
        from arcllm.adapters.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(FAKE_CONFIG, FAKE_MODEL)
        for reason in ["end_turn", "tool_use", "max_tokens", "stop_sequence"]:
            data = _make_anthropic_text_response(stop_reason=reason)
            resp = adapter._parse_response(data)
            assert resp.stop_reason == reason

    def test_raw_response_stored(self):
        from arcllm.adapters.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(FAKE_CONFIG, FAKE_MODEL)
        data = _make_anthropic_text_response()
        resp = adapter._parse_response(data)
        assert resp.raw is data

    def test_thinking_response(self):
        from arcllm.adapters.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(FAKE_CONFIG, FAKE_MODEL)
        data = _make_anthropic_text_response()
        data["content"] = [
            {"type": "thinking", "thinking": "Let me think about this..."},
            {"type": "text", "text": "Here's my answer."},
        ]
        resp = adapter._parse_response(data)
        assert resp.thinking == "Let me think about this..."
        assert resp.content == "Here's my answer."


class TestAnthropicToolCallParsing:
    def test_tool_call_dict_arguments(self):
        from arcllm.adapters.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(FAKE_CONFIG, FAKE_MODEL)
        block = {"type": "tool_use", "id": "t1", "name": "calc", "input": {"x": 1}}
        tc = adapter._parse_tool_call(block)
        assert tc.arguments == {"x": 1}

    def test_tool_call_string_arguments(self):
        from arcllm.adapters.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(FAKE_CONFIG, FAKE_MODEL)
        block = {
            "type": "tool_use",
            "id": "t1",
            "name": "calc",
            "input": '{"x": 1}',
        }
        tc = adapter._parse_tool_call(block)
        assert tc.arguments == {"x": 1}

    def test_tool_call_bad_arguments(self):
        from arcllm.adapters.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(FAKE_CONFIG, FAKE_MODEL)
        block = {
            "type": "tool_use",
            "id": "t1",
            "name": "calc",
            "input": "not valid json {{{",
        }
        with pytest.raises(ArcLLMParseError):
            adapter._parse_tool_call(block)

    def test_tool_call_unexpected_input_type(self):
        from arcllm.adapters.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(FAKE_CONFIG, FAKE_MODEL)
        block = {
            "type": "tool_use",
            "id": "t1",
            "name": "calc",
            "input": 12345,
        }
        with pytest.raises(ArcLLMParseError):
            adapter._parse_tool_call(block)


class TestAnthropicErrorHandling:
    @pytest.mark.asyncio
    async def test_http_429_error(self):
        from arcllm.adapters.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(FAKE_CONFIG, FAKE_MODEL)
        mock_response = httpx.Response(
            429,
            text="rate limited",
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        )
        adapter._client = AsyncMock()
        adapter._client.post = AsyncMock(return_value=mock_response)

        with pytest.raises(ArcLLMAPIError) as exc_info:
            await adapter.invoke([Message(role="user", content="Hi")])
        assert exc_info.value.status_code == 429
        assert exc_info.value.provider == "anthropic"

    @pytest.mark.asyncio
    async def test_http_401_error(self):
        from arcllm.adapters.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(FAKE_CONFIG, FAKE_MODEL)
        mock_response = httpx.Response(
            401,
            text="unauthorized",
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        )
        adapter._client = AsyncMock()
        adapter._client.post = AsyncMock(return_value=mock_response)

        with pytest.raises(ArcLLMAPIError) as exc_info:
            await adapter.invoke([Message(role="user", content="Hi")])
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_http_500_error(self):
        from arcllm.adapters.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(FAKE_CONFIG, FAKE_MODEL)
        mock_response = httpx.Response(
            500,
            text="internal server error",
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        )
        adapter._client = AsyncMock()
        adapter._client.post = AsyncMock(return_value=mock_response)

        with pytest.raises(ArcLLMAPIError) as exc_info:
            await adapter.invoke([Message(role="user", content="Hi")])
        assert exc_info.value.status_code == 500


class TestAnthropicFullCycle:
    @pytest.mark.asyncio
    async def test_complete_text_cycle(self):
        from arcllm.adapters.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(FAKE_CONFIG, FAKE_MODEL)
        response_data = _make_anthropic_text_response(text="Hello!")
        mock_response = httpx.Response(
            200,
            json=response_data,
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
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
        from arcllm.adapters.anthropic import AnthropicAdapter

        adapter = AnthropicAdapter(FAKE_CONFIG, FAKE_MODEL)
        response_data = _make_anthropic_tool_response(
            tool_id="t1", tool_name="search", tool_input={"query": "cats"}
        )
        mock_response = httpx.Response(
            200,
            json=response_data,
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
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
