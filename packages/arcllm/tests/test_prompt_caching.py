"""SPEC-029 Stream A — provider prompt caching.

Anthropic breakpoint placement (write side) and OpenAI/Gemini cache-token
read-back (telemetry side). Covers REQ-001..REQ-005.
"""

import pytest

from arcllm.adapters.anthropic import AnthropicAdapter
from arcllm.adapters.openai import OpenaiAdapter, _parse_openai_sse_line
from arcllm.config import ModelMetadata, ProviderConfig, ProviderSettings
from arcllm.types import Message, TextBlock, Tool

FAKE_MODEL = "claude-test-1"


def _config(*, enable_caching: bool = True, ttl: str = "5m", api_format: str = "anthropic") -> ProviderConfig:
    return ProviderConfig(
        provider=ProviderSettings(
            api_format=api_format,
            base_url="https://api.anthropic.com",
            api_key_env="ARCLLM_TEST_KEY",
            default_model=FAKE_MODEL,
            default_temperature=0.7,
            enable_prompt_caching=enable_caching,
            cache_ttl=ttl,
        ),
        models={
            FAKE_MODEL: ModelMetadata(
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
        },
    )


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("ARCLLM_TEST_KEY", "test-key")


# --- Anthropic write side (REQ-001/002/003) --------------------------------


class TestAnthropicBreakpoints:
    def _messages_tools(self):
        messages = [
            Message(role="system", content="You are a helpful agent."),
            Message(role="user", content="Hello"),
        ]
        tools = [
            Tool(name="a", description="tool a", parameters={"type": "object"}),
            Tool(name="b", description="tool b", parameters={"type": "object"}),
        ]
        return messages, tools

    def test_caching_on_places_three_breakpoints(self):
        adapter = AnthropicAdapter(_config(enable_caching=True), FAKE_MODEL)
        messages, tools = self._messages_tools()
        body = adapter._build_request_body(messages, tools=tools)

        # system is a content-block list carrying a breakpoint
        assert isinstance(body["system"], list)
        assert body["system"][-1]["cache_control"] == {"type": "ephemeral"}
        # breakpoint on the LAST tool only
        assert "cache_control" not in body["tools"][0]
        assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
        # breakpoint on the last block of the last message
        last_content = body["messages"][-1]["content"]
        assert isinstance(last_content, list)
        assert last_content[-1]["cache_control"] == {"type": "ephemeral"}

    def test_caching_off_keeps_plain_string_system_and_no_markers(self):
        adapter = AnthropicAdapter(_config(enable_caching=False), FAKE_MODEL)
        messages, tools = self._messages_tools()
        body = adapter._build_request_body(messages, tools=tools)

        assert body["system"] == "You are a helpful agent."
        assert all("cache_control" not in t for t in body["tools"])
        assert isinstance(body["messages"][-1]["content"], str)

    def test_one_hour_ttl_marker(self):
        adapter = AnthropicAdapter(_config(enable_caching=True, ttl="1h"), FAKE_MODEL)
        messages, tools = self._messages_tools()
        body = adapter._build_request_body(messages, tools=tools)
        assert body["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

    def test_no_tools_still_breaks_system_and_message(self):
        adapter = AnthropicAdapter(_config(enable_caching=True), FAKE_MODEL)
        messages = [
            Message(role="system", content="sys"),
            Message(role="user", content=[TextBlock(text="hi")]),
        ]
        body = adapter._build_request_body(messages)
        assert "tools" not in body
        assert body["system"][-1]["cache_control"] == {"type": "ephemeral"}
        assert body["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}

    def test_usage_reads_cache_tokens(self):
        adapter = AnthropicAdapter(_config(), FAKE_MODEL)
        usage = adapter._parse_usage(
            {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 512,
                "cache_creation_input_tokens": 64,
            }
        )
        assert usage.cache_read_tokens == 512
        assert usage.cache_write_tokens == 64


# --- OpenAI / Gemini read side (REQ-004/005) -------------------------------


class TestOpenAICacheTelemetry:
    def _adapter(self):
        return OpenaiAdapter(_config(api_format="openai"), FAKE_MODEL)

    def test_cached_tokens_mapped(self):
        usage = self._adapter()._parse_usage(
            {
                "prompt_tokens": 2000,
                "completion_tokens": 50,
                "total_tokens": 2050,
                "prompt_tokens_details": {"cached_tokens": 1536},
            }
        )
        assert usage.cache_read_tokens == 1536

    def test_cache_miss_is_zero_not_none(self):
        usage = self._adapter()._parse_usage(
            {
                "prompt_tokens": 2000,
                "completion_tokens": 50,
                "total_tokens": 2050,
                "prompt_tokens_details": {"cached_tokens": 0},
            }
        )
        assert usage.cache_read_tokens == 0

    def test_field_absent_is_none(self):
        usage = self._adapter()._parse_usage(
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        )
        assert usage.cache_read_tokens is None

    def test_streaming_usage_reads_cached_tokens(self):
        line = (
            'data: {"choices": [], "usage": {"prompt_tokens": 2000, '
            '"completion_tokens": 50, "total_tokens": 2050, '
            '"prompt_tokens_details": {"cached_tokens": 1536}}}'
        )
        delta = _parse_openai_sse_line(line)
        assert delta is not None
        assert delta.usage is not None
        assert delta.usage.cache_read_tokens == 1536

    def test_google_adapter_does_not_fork_parsing(self):
        from arcllm.adapters.google import GoogleAdapter

        assert "_parse_usage" not in vars(GoogleAdapter)
