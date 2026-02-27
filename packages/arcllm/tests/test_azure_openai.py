"""Tests for Azure OpenAI Service adapter (SPEC-010).

Covers Azure-specific behavior: api-key header auth, v1 URL construction,
deployment name as model, content filter handling. Inherited OpenAI behavior
(tool calling, response parsing) is tested in test_open_providers.py.
"""

from unittest.mock import AsyncMock

import httpx
import pytest

from arcllm.config import ModelMetadata, ProviderConfig, ProviderSettings
from arcllm.exceptions import ArcLLMAPIError
from arcllm.types import LLMResponse, Message


def _make_azure_config(
    base_url: str = "https://myresource.openai.azure.us",
    api_key_env: str = "AZURE_OPENAI_API_KEY",
) -> ProviderConfig:
    """Build a minimal Azure OpenAI ProviderConfig for testing."""
    return ProviderConfig(
        provider=ProviderSettings(
            api_format="openai-chat",
            base_url=base_url,
            api_key_env=api_key_env,
            api_key_required=True,
            default_model="my-gpt4o-deployment",
            default_temperature=0.7,
        ),
        models={
            "my-gpt4o-deployment": ModelMetadata(
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
        },
    )


def _make_openai_response(
    content: str | None = "Hello!",
    finish_reason: str = "stop",
    model: str = "my-gpt4o-deployment",
) -> dict:
    """Build a minimal OpenAI-format chat completion response."""
    return {
        "id": "chatcmpl-azure-test",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
    }


class TestAzureOpenaiName:
    """Adapter name property returns azure_openai."""

    def test_name_property(self, monkeypatch):
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
        from arcllm.adapters.azure_openai import Azure_OpenaiAdapter

        config = _make_azure_config()
        adapter = Azure_OpenaiAdapter(config, "my-gpt4o-deployment")
        assert adapter.name == "azure_openai"


class TestAzureOpenaiHeaders:
    """Azure uses api-key header instead of Authorization: Bearer."""

    def test_headers_use_api_key(self, monkeypatch):
        """_build_headers() returns api-key header (not Authorization)."""
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-test-key-123")
        from arcllm.adapters.azure_openai import Azure_OpenaiAdapter

        config = _make_azure_config()
        adapter = Azure_OpenaiAdapter(config, "my-gpt4o-deployment")
        headers = adapter._build_headers()

        assert headers["api-key"] == "azure-test-key-123"
        assert headers["Content-Type"] == "application/json"

    def test_headers_no_authorization(self, monkeypatch):
        """_build_headers() must NOT include Authorization key."""
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-test-key-123")
        from arcllm.adapters.azure_openai import Azure_OpenaiAdapter

        config = _make_azure_config()
        adapter = Azure_OpenaiAdapter(config, "my-gpt4o-deployment")
        headers = adapter._build_headers()

        assert "Authorization" not in headers

    def test_headers_no_api_key_when_empty(self, monkeypatch):
        """When resolved key is empty string, api-key header should be absent."""
        from arcllm.adapters.azure_openai import Azure_OpenaiAdapter

        config = _make_azure_config()
        # Pass empty string as resolved key to bypass env lookup
        adapter = Azure_OpenaiAdapter(config, "my-gpt4o-deployment", resolved_api_key="")
        headers = adapter._build_headers()

        assert "api-key" not in headers
        assert "Authorization" not in headers


class TestAzureOpenaiURL:
    """Azure v1 API URL construction."""

    @pytest.mark.asyncio
    async def test_url_uses_v1_path(self, monkeypatch):
        """invoke() constructs URL with /openai/v1/chat/completions path."""
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
        from arcllm.adapters.azure_openai import Azure_OpenaiAdapter

        config = _make_azure_config(base_url="https://myresource.openai.azure.us")
        adapter = Azure_OpenaiAdapter(config, "my-gpt4o-deployment")

        mock_response = httpx.Response(
            status_code=200,
            json=_make_openai_response(),
        )
        adapter._client.post = AsyncMock(return_value=mock_response)

        messages = [Message(role="user", content="Hello")]
        await adapter.invoke(messages)

        call_args = adapter._client.post.call_args
        url = call_args[0][0]
        assert url == "https://myresource.openai.azure.us/openai/v1/chat/completions"

    @pytest.mark.asyncio
    async def test_url_no_query_parameters(self, monkeypatch):
        """v1 API URL must have no query parameters (api-version causes 400)."""
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
        from arcllm.adapters.azure_openai import Azure_OpenaiAdapter

        config = _make_azure_config()
        adapter = Azure_OpenaiAdapter(config, "my-gpt4o-deployment")

        mock_response = httpx.Response(
            status_code=200,
            json=_make_openai_response(),
        )
        adapter._client.post = AsyncMock(return_value=mock_response)

        messages = [Message(role="user", content="Hello")]
        await adapter.invoke(messages)

        call_args = adapter._client.post.call_args
        url = call_args[0][0]
        assert "?" not in url

    @pytest.mark.asyncio
    async def test_url_with_trailing_slash_base(self, monkeypatch):
        """Base URL with trailing slash should not produce double slash."""
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
        from arcllm.adapters.azure_openai import Azure_OpenaiAdapter

        config = _make_azure_config(base_url="https://myresource.openai.azure.us/")
        adapter = Azure_OpenaiAdapter(config, "my-gpt4o-deployment")

        mock_response = httpx.Response(
            status_code=200,
            json=_make_openai_response(),
        )
        adapter._client.post = AsyncMock(return_value=mock_response)

        messages = [Message(role="user", content="Hello")]
        await adapter.invoke(messages)

        call_args = adapter._client.post.call_args
        url = call_args[0][0]
        assert "//" not in url.replace("https://", "")


class TestAzureOpenaiDeploymentName:
    """Model field in request body maps to deployment name."""

    @pytest.mark.asyncio
    async def test_deployment_name_in_body(self, monkeypatch):
        """Request body model field should be the deployment name."""
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
        from arcllm.adapters.azure_openai import Azure_OpenaiAdapter

        config = _make_azure_config()
        adapter = Azure_OpenaiAdapter(config, "my-custom-deployment")

        mock_response = httpx.Response(
            status_code=200,
            json=_make_openai_response(),
        )
        adapter._client.post = AsyncMock(return_value=mock_response)

        messages = [Message(role="user", content="Hello")]
        await adapter.invoke(messages)

        call_args = adapter._client.post.call_args
        body = call_args[1]["json"]
        assert body["model"] == "my-custom-deployment"


class TestAzureOpenaiContentFilter:
    """Azure Content Safety filter scenarios."""

    @pytest.mark.asyncio
    async def test_content_filter_finish_reason(self, monkeypatch):
        """Output blocked: content=null, finish_reason=content_filter."""
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
        from arcllm.adapters.azure_openai import Azure_OpenaiAdapter

        config = _make_azure_config()
        adapter = Azure_OpenaiAdapter(config, "my-gpt4o-deployment")

        mock_response = httpx.Response(
            status_code=200,
            json=_make_openai_response(content=None, finish_reason="content_filter"),
        )
        adapter._client.post = AsyncMock(return_value=mock_response)

        messages = [Message(role="user", content="Hello")]
        response = await adapter.invoke(messages)

        assert isinstance(response, LLMResponse)
        assert response.content is None
        assert response.stop_reason == "content_filter"

    @pytest.mark.asyncio
    async def test_prompt_blocked_raises_error(self, monkeypatch):
        """Prompt blocked: HTTP 400 raises ArcLLMAPIError."""
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
        from arcllm.adapters.azure_openai import Azure_OpenaiAdapter

        config = _make_azure_config()
        adapter = Azure_OpenaiAdapter(config, "my-gpt4o-deployment")

        mock_response = httpx.Response(
            status_code=400,
            text='{"error": {"code": "ResponsibleAIPolicyViolation"}}',
        )
        adapter._client.post = AsyncMock(return_value=mock_response)

        messages = [Message(role="user", content="Blocked content")]
        with pytest.raises(ArcLLMAPIError) as exc_info:
            await adapter.invoke(messages)

        assert exc_info.value.status_code == 400
        assert exc_info.value.provider == "azure_openai"


class TestAzureOpenaiErrorHandling:
    """Azure-specific error responses."""

    @pytest.mark.asyncio
    async def test_429_extracts_retry_after(self, monkeypatch):
        """Rate limited: 429 with Retry-After header."""
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
        from arcllm.adapters.azure_openai import Azure_OpenaiAdapter

        config = _make_azure_config()
        adapter = Azure_OpenaiAdapter(config, "my-gpt4o-deployment")

        mock_response = httpx.Response(
            status_code=429,
            headers={"retry-after": "30"},
            text='{"error": {"code": "TooManyRequests"}}',
        )
        adapter._client.post = AsyncMock(return_value=mock_response)

        messages = [Message(role="user", content="Hello")]
        with pytest.raises(ArcLLMAPIError) as exc_info:
            await adapter.invoke(messages)

        assert exc_info.value.status_code == 429
        assert exc_info.value.retry_after == 30.0

    @pytest.mark.asyncio
    async def test_404_deployment_not_found(self, monkeypatch):
        """DeploymentNotFound: 404 during propagation window."""
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
        from arcllm.adapters.azure_openai import Azure_OpenaiAdapter

        config = _make_azure_config()
        adapter = Azure_OpenaiAdapter(config, "my-gpt4o-deployment")

        mock_response = httpx.Response(
            status_code=404,
            text='{"error": {"code": "DeploymentNotFound"}}',
        )
        adapter._client.post = AsyncMock(return_value=mock_response)

        messages = [Message(role="user", content="Hello")]
        with pytest.raises(ArcLLMAPIError) as exc_info:
            await adapter.invoke(messages)

        assert exc_info.value.status_code == 404


class TestAzureOpenaiInvoke:
    """Full invoke round-trip."""

    @pytest.mark.asyncio
    async def test_successful_invoke(self, monkeypatch):
        """Standard successful invoke parses response correctly."""
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
        from arcllm.adapters.azure_openai import Azure_OpenaiAdapter

        config = _make_azure_config()
        adapter = Azure_OpenaiAdapter(config, "my-gpt4o-deployment")

        mock_response = httpx.Response(
            status_code=200,
            json=_make_openai_response("Hello from Azure!"),
        )
        adapter._client.post = AsyncMock(return_value=mock_response)

        messages = [Message(role="user", content="Hello")]
        response = await adapter.invoke(messages)

        assert isinstance(response, LLMResponse)
        assert response.content == "Hello from Azure!"
        assert response.stop_reason == "end_turn"
        assert response.usage.total_tokens == 15
