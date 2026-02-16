"""Tests for ArcLLM open-model provider adapters (Step 15).

Parametrized tests covering all 9 new providers: Ollama, vLLM, Together, Groq,
Fireworks, DeepSeek, HuggingFace, HuggingFace TGI, plus Mistral (basic).
"""

import importlib
from unittest.mock import AsyncMock

import httpx
import pytest

from arcllm.config import (
    ModelMetadata,
    ProviderConfig,
    ProviderSettings,
    load_provider_config,
)
from arcllm.exceptions import ArcLLMConfigError
from arcllm.registry import clear_cache, load_model
from arcllm.types import LLMResponse, Message


# ---------------------------------------------------------------------------
# Provider metadata for parametrization
# ---------------------------------------------------------------------------

# (provider_name, api_key_env, api_key_required, expected_base_url_prefix)
LOCAL_PROVIDERS = [
    ("ollama", "OLLAMA_API_KEY", False, "http://localhost"),
    ("vllm", "VLLM_API_KEY", False, "http://localhost"),
    ("huggingface_tgi", "TGI_API_KEY", False, "http://localhost"),
]

CLOUD_PROVIDERS = [
    ("together", "TOGETHER_API_KEY", True, "https://api.together.xyz"),
    ("groq", "GROQ_API_KEY", True, "https://api.groq.com"),
    ("fireworks", "FIREWORKS_API_KEY", True, "https://api.fireworks.ai"),
    ("deepseek", "DEEPSEEK_API_KEY", True, "https://api.deepseek.com"),
    ("huggingface", "HF_TOKEN", True, "https://api-inference.huggingface.co"),
    ("mistral", "MISTRAL_API_KEY", True, "https://api.mistral.ai"),
]

ALL_PROVIDERS = LOCAL_PROVIDERS + CLOUD_PROVIDERS

# Expected adapter class names by convention: provider_name.title() + "Adapter"
EXPECTED_NAMES = {
    "ollama": "ollama",
    "vllm": "vllm",
    "together": "together",
    "groq": "groq",
    "fireworks": "fireworks",
    "deepseek": "deepseek",
    "mistral": "mistral",
    "huggingface": "huggingface",
    "huggingface_tgi": "huggingface_tgi",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear registry cache before/after each test."""
    clear_cache()
    yield
    clear_cache()


def _make_fake_config(
    provider_name: str,
    api_key_env: str = "ARCLLM_TEST_KEY",
    api_key_required: bool = True,
    base_url: str = "https://test.example.com",
) -> ProviderConfig:
    """Build a minimal ProviderConfig for testing."""
    return ProviderConfig(
        provider=ProviderSettings(
            api_format="openai-chat",
            base_url=base_url,
            api_key_env=api_key_env,
            api_key_required=api_key_required,
            default_model="test-model",
            default_temperature=0.7,
        ),
        models={
            "test-model": ModelMetadata(
                context_window=4096,
                max_output_tokens=2048,
                supports_tools=True,
                supports_vision=False,
                supports_thinking=False,
                input_modalities=["text"],
                cost_input_per_1m=0.0,
                cost_output_per_1m=0.0,
                cost_cache_read_per_1m=0.0,
                cost_cache_write_per_1m=0.0,
            )
        },
    )


def _get_adapter_class(provider_name: str):
    """Load adapter class by convention: provider_name.title() + 'Adapter'."""
    mod = importlib.import_module(f"arcllm.adapters.{provider_name}")
    return getattr(mod, f"{provider_name.title()}Adapter")


def _make_openai_text_response(
    text: str = "Hello!",
    model: str = "test-model",
) -> dict:
    """Build a minimal OpenAI-format chat completion response."""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
    }


# ---------------------------------------------------------------------------
# Config loading tests
# ---------------------------------------------------------------------------


class TestProviderConfigLoading:
    """Verify all 9 provider TOMLs load correctly."""

    @pytest.mark.parametrize(
        "provider_name,api_key_env,api_key_required,base_url_prefix",
        ALL_PROVIDERS,
        ids=[p[0] for p in ALL_PROVIDERS],
    )
    def test_toml_loads_successfully(
        self, provider_name, api_key_env, api_key_required, base_url_prefix
    ):
        config = load_provider_config(provider_name)
        assert config.provider.api_key_env == api_key_env
        assert config.provider.api_key_required is api_key_required
        assert config.provider.base_url.startswith(base_url_prefix)

    @pytest.mark.parametrize(
        "provider_name,api_key_env,api_key_required,base_url_prefix",
        ALL_PROVIDERS,
        ids=[p[0] for p in ALL_PROVIDERS],
    )
    def test_toml_has_models(
        self, provider_name, api_key_env, api_key_required, base_url_prefix
    ):
        config = load_provider_config(provider_name)
        assert len(config.models) >= 1, f"{provider_name} should have at least 1 model"

    @pytest.mark.parametrize(
        "provider_name,api_key_env,api_key_required,base_url_prefix",
        LOCAL_PROVIDERS,
        ids=[p[0] for p in LOCAL_PROVIDERS],
    )
    def test_local_providers_zero_cost(
        self, provider_name, api_key_env, api_key_required, base_url_prefix
    ):
        config = load_provider_config(provider_name)
        for model_name, meta in config.models.items():
            assert meta.cost_input_per_1m == 0.0, f"{provider_name}/{model_name}"
            assert meta.cost_output_per_1m == 0.0, f"{provider_name}/{model_name}"


# ---------------------------------------------------------------------------
# Auth handling tests
# ---------------------------------------------------------------------------


class TestOptionalAuth:
    """Verify api_key_required=false behavior."""

    @pytest.mark.parametrize(
        "provider_name,api_key_env,api_key_required,base_url_prefix",
        LOCAL_PROVIDERS,
        ids=[p[0] for p in LOCAL_PROVIDERS],
    )
    def test_local_provider_no_key_no_error(
        self, provider_name, api_key_env, api_key_required, base_url_prefix, monkeypatch
    ):
        """Local providers should not raise when API key env var is unset."""
        monkeypatch.delenv(api_key_env, raising=False)
        config = _make_fake_config(
            provider_name,
            api_key_env=api_key_env,
            api_key_required=False,
            base_url=base_url_prefix,
        )
        # Should NOT raise
        cls = _get_adapter_class(provider_name)
        adapter = cls(config, "test-model")
        assert adapter._api_key == ""

    @pytest.mark.parametrize(
        "provider_name,api_key_env,api_key_required,base_url_prefix",
        LOCAL_PROVIDERS,
        ids=[p[0] for p in LOCAL_PROVIDERS],
    )
    def test_local_provider_optional_key_used(
        self, provider_name, api_key_env, api_key_required, base_url_prefix, monkeypatch
    ):
        """When API key IS set for a local provider, it should be stored."""
        monkeypatch.setenv(api_key_env, "optional-test-key")
        config = _make_fake_config(
            provider_name,
            api_key_env=api_key_env,
            api_key_required=False,
            base_url=base_url_prefix,
        )
        cls = _get_adapter_class(provider_name)
        adapter = cls(config, "test-model")
        assert adapter._api_key == "optional-test-key"

    @pytest.mark.parametrize(
        "provider_name,api_key_env,api_key_required,base_url_prefix",
        CLOUD_PROVIDERS,
        ids=[p[0] for p in CLOUD_PROVIDERS],
    )
    def test_cloud_provider_missing_key_raises(
        self, provider_name, api_key_env, api_key_required, base_url_prefix, monkeypatch
    ):
        """Cloud providers should raise when API key env var is unset."""
        monkeypatch.delenv(api_key_env, raising=False)
        config = _make_fake_config(
            provider_name,
            api_key_env=api_key_env,
            api_key_required=True,
            base_url=base_url_prefix,
        )
        cls = _get_adapter_class(provider_name)
        with pytest.raises(ArcLLMConfigError, match="Missing environment variable"):
            cls(config, "test-model")


# ---------------------------------------------------------------------------
# Header tests
# ---------------------------------------------------------------------------


class TestAuthHeaders:
    """Verify Authorization header is conditional on API key presence."""

    def test_no_auth_header_when_no_key(self, monkeypatch):
        """When api_key is empty, no Authorization header should be sent."""
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
        config = _make_fake_config(
            "ollama",
            api_key_env="OLLAMA_API_KEY",
            api_key_required=False,
            base_url="http://localhost:11434",
        )
        from arcllm.adapters.ollama import OllamaAdapter

        adapter = OllamaAdapter(config, "test-model")
        headers = adapter._build_headers()
        assert "Authorization" not in headers
        assert headers["Content-Type"] == "application/json"

    def test_auth_header_when_key_set(self, monkeypatch):
        """When api_key IS set (even for local), Authorization header should be sent."""
        monkeypatch.setenv("OLLAMA_API_KEY", "my-optional-key")
        config = _make_fake_config(
            "ollama",
            api_key_env="OLLAMA_API_KEY",
            api_key_required=False,
            base_url="http://localhost:11434",
        )
        from arcllm.adapters.ollama import OllamaAdapter

        adapter = OllamaAdapter(config, "test-model")
        headers = adapter._build_headers()
        assert headers["Authorization"] == "Bearer my-optional-key"

    def test_cloud_auth_header(self, monkeypatch):
        """Cloud providers should always include Authorization."""
        monkeypatch.setenv("GROQ_API_KEY", "groq-test-key")
        config = _make_fake_config(
            "groq",
            api_key_env="GROQ_API_KEY",
            api_key_required=True,
            base_url="https://api.groq.com/openai",
        )
        from arcllm.adapters.groq import GroqAdapter

        adapter = GroqAdapter(config, "test-model")
        headers = adapter._build_headers()
        assert headers["Authorization"] == "Bearer groq-test-key"


# ---------------------------------------------------------------------------
# Adapter name property tests
# ---------------------------------------------------------------------------


class TestAdapterNames:
    """Each adapter must return its correct provider name."""

    @pytest.mark.parametrize(
        "provider_name,api_key_env,api_key_required,base_url_prefix",
        ALL_PROVIDERS,
        ids=[p[0] for p in ALL_PROVIDERS],
    )
    def test_name_property(
        self, provider_name, api_key_env, api_key_required, base_url_prefix, monkeypatch
    ):
        monkeypatch.setenv(api_key_env, "test-key")
        config = _make_fake_config(
            provider_name,
            api_key_env=api_key_env,
            api_key_required=api_key_required,
            base_url=base_url_prefix,
        )
        cls = _get_adapter_class(provider_name)
        adapter = cls(config, "test-model")
        assert adapter.name == EXPECTED_NAMES[provider_name]


# ---------------------------------------------------------------------------
# Invoke tests (basic — inherited from OpenaiAdapter)
# ---------------------------------------------------------------------------


class TestBasicInvoke:
    """Verify that alias adapters can invoke and parse responses."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "provider_name,api_key_env,api_key_required,base_url_prefix",
        ALL_PROVIDERS,
        ids=[p[0] for p in ALL_PROVIDERS],
    )
    async def test_invoke_text_response(
        self, provider_name, api_key_env, api_key_required, base_url_prefix, monkeypatch
    ):
        """Each adapter should parse a standard OpenAI text response."""
        monkeypatch.setenv(api_key_env, "test-key")
        config = _make_fake_config(
            provider_name,
            api_key_env=api_key_env,
            api_key_required=api_key_required,
            base_url=base_url_prefix,
        )
        cls = _get_adapter_class(provider_name)
        adapter = cls(config, "test-model")

        # Mock the httpx client
        mock_response = httpx.Response(
            status_code=200,
            json=_make_openai_text_response("Hi from adapter!"),
        )
        adapter._client.post = AsyncMock(return_value=mock_response)

        messages = [Message(role="user", content="Hello")]
        response = await adapter.invoke(messages)

        assert isinstance(response, LLMResponse)
        assert response.content == "Hi from adapter!"
        assert response.stop_reason == "end_turn"
        assert response.usage.total_tokens == 15


# ---------------------------------------------------------------------------
# Registry integration tests
# ---------------------------------------------------------------------------


class TestRegistryIntegration:
    """Verify load_model() works for all new providers."""

    @pytest.mark.parametrize(
        "provider_name,api_key_env,api_key_required,base_url_prefix",
        ALL_PROVIDERS,
        ids=[p[0] for p in ALL_PROVIDERS],
    )
    def test_load_model_discovers_adapter(
        self, provider_name, api_key_env, api_key_required, base_url_prefix, monkeypatch
    ):
        """Convention-based registry should discover and load each adapter."""
        monkeypatch.setenv(api_key_env, "test-key")
        model = load_model(provider_name)
        assert model.name == EXPECTED_NAMES[provider_name]

    @pytest.mark.parametrize(
        "provider_name,api_key_env,api_key_required,base_url_prefix",
        LOCAL_PROVIDERS,
        ids=[p[0] for p in LOCAL_PROVIDERS],
    )
    def test_load_model_local_no_key(
        self, provider_name, api_key_env, api_key_required, base_url_prefix, monkeypatch
    ):
        """load_model() should work for local providers without API key."""
        monkeypatch.delenv(api_key_env, raising=False)
        model = load_model(provider_name)
        assert model.name == EXPECTED_NAMES[provider_name]


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Existing providers still work as before."""

    def test_anthropic_still_requires_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        config = load_provider_config("anthropic")
        assert config.provider.api_key_required is True

    def test_openai_still_requires_key(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        config = load_provider_config("openai")
        assert config.provider.api_key_required is True

    def test_anthropic_missing_key_raises(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        from arcllm.adapters.anthropic import AnthropicAdapter

        config = load_provider_config("anthropic")
        with pytest.raises(ArcLLMConfigError):
            AnthropicAdapter(config, "claude-sonnet-4-20250514")


# ---------------------------------------------------------------------------
# validate_config tests
# ---------------------------------------------------------------------------


class TestValidateConfig:
    """validate_config() should respect api_key_required flag."""

    @pytest.mark.parametrize(
        "provider_name,api_key_env,api_key_required,base_url_prefix",
        LOCAL_PROVIDERS,
        ids=[p[0] for p in LOCAL_PROVIDERS],
    )
    def test_local_provider_valid_without_key(
        self, provider_name, api_key_env, api_key_required, base_url_prefix, monkeypatch
    ):
        """Local providers without API key should validate as True."""
        monkeypatch.delenv(api_key_env, raising=False)
        config = _make_fake_config(
            provider_name,
            api_key_env=api_key_env,
            api_key_required=False,
            base_url=base_url_prefix,
        )
        cls = _get_adapter_class(provider_name)
        adapter = cls(config, "test-model")
        assert adapter.validate_config() is True

    @pytest.mark.parametrize(
        "provider_name,api_key_env,api_key_required,base_url_prefix",
        CLOUD_PROVIDERS,
        ids=[p[0] for p in CLOUD_PROVIDERS],
    )
    def test_cloud_provider_valid_with_key(
        self, provider_name, api_key_env, api_key_required, base_url_prefix, monkeypatch
    ):
        """Cloud providers with API key should validate as True."""
        monkeypatch.setenv(api_key_env, "test-key")
        config = _make_fake_config(
            provider_name,
            api_key_env=api_key_env,
            api_key_required=True,
            base_url=base_url_prefix,
        )
        cls = _get_adapter_class(provider_name)
        adapter = cls(config, "test-model")
        assert adapter.validate_config() is True


# ---------------------------------------------------------------------------
# BaseAdapter edge case tests
# ---------------------------------------------------------------------------


class TestBaseAdapterEdgeCases:
    """Cover uncovered branches in BaseAdapter."""

    def test_name_fallback_uses_api_format(self, monkeypatch):
        """BaseAdapter.name returns api_format when not overridden by subclass."""
        monkeypatch.setenv("ARCLLM_TEST_KEY", "test-key")
        from arcllm.adapters.base import BaseAdapter
        from arcllm.types import LLMResponse, Message, Tool

        class _StubAdapter(BaseAdapter):
            """Minimal concrete subclass that does not override name."""

            async def invoke(
                self, messages: list[Message], tools: list[Tool] | None = None, **kwargs
            ) -> LLMResponse:
                raise NotImplementedError

        config = _make_fake_config("test", api_key_env="ARCLLM_TEST_KEY")
        adapter = _StubAdapter(config, "test-model")
        # BaseAdapter.name returns api_format from config
        assert adapter.name == "openai-chat"

    def test_parse_retry_after_non_numeric(self):
        """Non-numeric Retry-After header returns None."""
        from arcllm.adapters.base import BaseAdapter

        response = httpx.Response(
            status_code=429,
            headers={"retry-after": "Thu, 01 Dec 2025 16:00:00 GMT"},
        )
        result = BaseAdapter._parse_retry_after(response)
        assert result is None

    def test_parse_retry_after_numeric(self):
        """Numeric Retry-After header returns float value."""
        from arcllm.adapters.base import BaseAdapter

        response = httpx.Response(
            status_code=429,
            headers={"retry-after": "30"},
        )
        result = BaseAdapter._parse_retry_after(response)
        assert result == 30.0

    def test_parse_retry_after_missing(self):
        """Missing Retry-After header returns None."""
        from arcllm.adapters.base import BaseAdapter

        response = httpx.Response(status_code=429)
        result = BaseAdapter._parse_retry_after(response)
        assert result is None


# ---------------------------------------------------------------------------
# Empty content block fallback test
# ---------------------------------------------------------------------------


class TestEmptyContentFallback:
    """Cover the empty content block path in OpenAI format_message."""

    def test_empty_content_blocks_returns_empty_string(self, monkeypatch):
        """Message with empty content block list should return empty string."""
        monkeypatch.setenv("ARCLLM_TEST_KEY", "test-key")
        from arcllm.adapters.openai import OpenaiAdapter

        config = _make_fake_config("test", api_key_env="ARCLLM_TEST_KEY")
        adapter = OpenaiAdapter(config, "test-model")
        # Message with empty list content — hits the `return {"role": ..., "content": ""}` branch
        msg = Message(role="user", content=[])
        result = adapter._format_message(msg)
        assert result["content"] == ""
