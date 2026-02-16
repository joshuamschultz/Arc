"""Tests for Mistral adapter quirk overrides."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from arcllm.adapters.mistral import MistralAdapter, _MISTRAL_STOP_REASON_MAP
from arcllm.config import ModelMetadata, ProviderConfig, ProviderSettings
from arcllm.types import LLMResponse, Message, Tool


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FAKE_MODEL = "mistral-large-test"

FAKE_CONFIG = ProviderConfig(
    provider=ProviderSettings(
        api_format="openai-chat",
        base_url="https://api.mistral.ai",
        api_key_env="ARCLLM_TEST_KEY",
        api_key_required=True,
        default_model=FAKE_MODEL,
        default_temperature=0.7,
    ),
    models={
        FAKE_MODEL: ModelMetadata(
            context_window=128000,
            max_output_tokens=8192,
            supports_tools=True,
            supports_vision=True,
            supports_thinking=False,
            input_modalities=["text", "image"],
            cost_input_per_1m=2.00,
            cost_output_per_1m=6.00,
            cost_cache_read_per_1m=0.0,
            cost_cache_write_per_1m=0.0,
        )
    },
)


@pytest.fixture(autouse=True)
def _set_test_api_key(monkeypatch):
    monkeypatch.setenv("ARCLLM_TEST_KEY", "test-mistral-key")


@pytest.fixture
def adapter():
    return MistralAdapter(FAKE_CONFIG, FAKE_MODEL)


def _make_response(
    text: str = "Hello!",
    finish_reason: str = "stop",
) -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": FAKE_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
    }


def _make_tool_response(
    tool_id: str = "call_01",
    tool_name: str = "search",
    tool_args: dict | None = None,
) -> dict:
    return {
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
                            "id": tool_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(tool_args or {"q": "test"}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
    }


# ---------------------------------------------------------------------------
# Name property
# ---------------------------------------------------------------------------


class TestMistralName:
    def test_name(self, adapter):
        assert adapter.name == "mistral"


# ---------------------------------------------------------------------------
# tool_choice translation
# ---------------------------------------------------------------------------


class TestToolChoiceTranslation:
    def test_required_maps_to_any(self, adapter):
        """tool_choice='required' should become 'any' for Mistral."""
        messages = [Message(role="user", content="Hello")]
        tools = [Tool(name="search", description="Search", parameters={"type": "object"})]

        body = adapter._build_request_body(messages, tools, tool_choice="required")
        assert body["tool_choice"] == "any"

    def test_auto_passes_through(self, adapter):
        """tool_choice='auto' should pass unchanged."""
        messages = [Message(role="user", content="Hello")]
        tools = [Tool(name="search", description="Search", parameters={"type": "object"})]

        body = adapter._build_request_body(messages, tools, tool_choice="auto")
        assert body["tool_choice"] == "auto"

    def test_none_passes_through(self, adapter):
        """tool_choice='none' should pass unchanged."""
        messages = [Message(role="user", content="Hello")]
        tools = [Tool(name="search", description="Search", parameters={"type": "object"})]

        body = adapter._build_request_body(messages, tools, tool_choice="none")
        assert body["tool_choice"] == "none"

    def test_dict_passes_through(self, adapter):
        """tool_choice with specific function should pass unchanged."""
        messages = [Message(role="user", content="Hello")]
        tools = [Tool(name="search", description="Search", parameters={"type": "object"})]
        specific = {"type": "function", "function": {"name": "search"}}

        body = adapter._build_request_body(messages, tools, tool_choice=specific)
        assert body["tool_choice"] == specific

    def test_no_tool_choice_no_field(self, adapter):
        """When tool_choice not provided, it should not appear in body."""
        messages = [Message(role="user", content="Hello")]
        body = adapter._build_request_body(messages)
        assert "tool_choice" not in body


# ---------------------------------------------------------------------------
# Stop reason mapping
# ---------------------------------------------------------------------------


class TestStopReasonMapping:
    def test_stop_maps_to_end_turn(self, adapter):
        assert adapter._map_stop_reason("stop") == "end_turn"

    def test_tool_calls_maps_to_tool_use(self, adapter):
        assert adapter._map_stop_reason("tool_calls") == "tool_use"

    def test_length_maps_to_max_tokens(self, adapter):
        assert adapter._map_stop_reason("length") == "max_tokens"

    def test_model_length_maps_to_max_tokens(self, adapter):
        """Mistral-specific: 'model_length' maps to 'max_tokens'."""
        assert adapter._map_stop_reason("model_length") == "max_tokens"

    def test_unknown_maps_to_end_turn(self, adapter):
        assert adapter._map_stop_reason("some_unknown_reason") == "end_turn"


# ---------------------------------------------------------------------------
# Full invoke cycle
# ---------------------------------------------------------------------------


class TestMistralInvoke:
    @pytest.mark.asyncio
    async def test_text_invoke(self, adapter):
        """Full text response cycle."""
        mock_response = httpx.Response(
            status_code=200,
            json=_make_response("Bonjour!"),
        )
        adapter._client.post = AsyncMock(return_value=mock_response)

        messages = [Message(role="user", content="Hello")]
        response = await adapter.invoke(messages)

        assert isinstance(response, LLMResponse)
        assert response.content == "Bonjour!"
        assert response.stop_reason == "end_turn"

    @pytest.mark.asyncio
    async def test_tool_invoke(self, adapter):
        """Full tool-calling response cycle."""
        mock_response = httpx.Response(
            status_code=200,
            json=_make_tool_response("call_42", "search", {"query": "weather"}),
        )
        adapter._client.post = AsyncMock(return_value=mock_response)

        tools = [Tool(name="search", description="Search", parameters={"type": "object"})]
        messages = [Message(role="user", content="Search weather")]
        response = await adapter.invoke(messages, tools, tool_choice="required")

        assert response.stop_reason == "tool_use"
        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].name == "search"

        # Verify the actual request body had "any" not "required"
        call_args = adapter._client.post.call_args
        request_body = call_args.kwargs["json"]
        assert request_body["tool_choice"] == "any"

    @pytest.mark.asyncio
    async def test_model_length_stop_reason(self, adapter):
        """Mistral-specific stop reason 'model_length'."""
        mock_response = httpx.Response(
            status_code=200,
            json=_make_response("Truncated...", finish_reason="model_length"),
        )
        adapter._client.post = AsyncMock(return_value=mock_response)

        messages = [Message(role="user", content="Tell me everything")]
        response = await adapter.invoke(messages)

        assert response.stop_reason == "max_tokens"
