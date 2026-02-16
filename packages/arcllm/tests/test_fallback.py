"""Tests for FallbackModule — provider chain on failure."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcllm.exceptions import ArcLLMAPIError, ArcLLMConfigError
from arcllm.modules.fallback import FallbackModule
from arcllm.types import LLMProvider, LLMResponse, Message, Usage

_OK_RESPONSE = LLMResponse(
    content="ok",
    usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
    model="test-model",
    stop_reason="end_turn",
)

_FALLBACK_RESPONSE = LLMResponse(
    content="fallback-ok",
    usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
    model="fallback-model",
    stop_reason="end_turn",
)


def _make_inner(side_effects):
    inner = MagicMock(spec=LLMProvider)
    inner.name = "test-provider"
    inner.model_name = "test-model"
    inner.validate_config.return_value = True
    inner.invoke = AsyncMock(side_effect=side_effects)
    return inner


def _api_error(status_code: int = 500) -> ArcLLMAPIError:
    return ArcLLMAPIError(status_code=status_code, body="error", provider="test")


@pytest.fixture
def messages():
    return [Message(role="user", content="hi")]


# ---------------------------------------------------------------------------
# TestFallbackSuccess
# ---------------------------------------------------------------------------


class TestFallbackSuccess:
    async def test_primary_succeeds_no_fallback(self, messages):
        inner = _make_inner([_OK_RESPONSE])
        config = {"chain": ["openai"]}
        module = FallbackModule(config, inner)
        result = await module.invoke(messages)
        assert result.content == "ok"

    @patch("arcllm.modules.fallback.load_model")
    async def test_primary_fails_first_fallback_succeeds(
        self, mock_load_model, messages
    ):
        inner = _make_inner([_api_error(500)])
        fallback_inner = _make_inner([_FALLBACK_RESPONSE])
        mock_load_model.return_value = fallback_inner

        config = {"chain": ["openai"]}
        module = FallbackModule(config, inner)
        result = await module.invoke(messages)

        assert result.content == "fallback-ok"
        mock_load_model.assert_called_once_with("openai")

    @patch("arcllm.modules.fallback.load_model")
    async def test_primary_fails_second_fallback_succeeds(
        self, mock_load_model, messages
    ):
        inner = _make_inner([_api_error(500)])
        fallback_1 = _make_inner([_api_error(500)])
        fallback_2 = _make_inner([_FALLBACK_RESPONSE])
        mock_load_model.side_effect = [fallback_1, fallback_2]

        config = {"chain": ["anthropic_backup", "openai"]}
        module = FallbackModule(config, inner)
        result = await module.invoke(messages)

        assert result.content == "fallback-ok"
        assert mock_load_model.call_count == 2


# ---------------------------------------------------------------------------
# TestFallbackExhaustion
# ---------------------------------------------------------------------------


class TestFallbackExhaustion:
    @patch("arcllm.modules.fallback.load_model")
    async def test_all_fallbacks_fail_raises_primary_error(
        self, mock_load_model, messages
    ):
        primary_error = _api_error(500)
        inner = _make_inner([primary_error])
        fallback_inner = _make_inner([_api_error(503)])
        mock_load_model.return_value = fallback_inner

        config = {"chain": ["openai"]}
        module = FallbackModule(config, inner)

        with pytest.raises(ArcLLMAPIError, match="500"):
            await module.invoke(messages)

    async def test_empty_chain_passes_through(self, messages):
        inner = _make_inner([_api_error(500)])
        config = {"chain": []}
        module = FallbackModule(config, inner)

        with pytest.raises(ArcLLMAPIError, match="500"):
            await module.invoke(messages)


# ---------------------------------------------------------------------------
# TestFallbackCreation
# ---------------------------------------------------------------------------


class TestFallbackCreation:
    @patch("arcllm.modules.fallback.load_model")
    async def test_fallback_adapter_created_via_load_model(
        self, mock_load_model, messages
    ):
        inner = _make_inner([_api_error(500)])
        fallback_inner = _make_inner([_FALLBACK_RESPONSE])
        mock_load_model.return_value = fallback_inner

        config = {"chain": ["openai"]}
        module = FallbackModule(config, inner)
        await module.invoke(messages)

        mock_load_model.assert_called_once_with("openai")

    @patch("arcllm.modules.fallback.load_model")
    async def test_fallback_adapter_created_on_demand(
        self, mock_load_model, messages
    ):
        """load_model not called when primary succeeds."""
        inner = _make_inner([_OK_RESPONSE])

        config = {"chain": ["openai"]}
        module = FallbackModule(config, inner)
        await module.invoke(messages)

        mock_load_model.assert_not_called()


# ---------------------------------------------------------------------------
# TestFallbackValidation
# ---------------------------------------------------------------------------


class TestFallbackValidation:
    def test_chain_too_long_rejected(self):
        inner = _make_inner([_OK_RESPONSE])
        config = {"chain": [f"provider_{i}" for i in range(11)]}
        with pytest.raises(ArcLLMConfigError, match="Fallback chain too long"):
            FallbackModule(config, inner)

    def test_max_chain_length_allowed(self):
        inner = _make_inner([_OK_RESPONSE])
        config = {"chain": [f"provider_{i}" for i in range(10)]}
        module = FallbackModule(config, inner)
        assert len(module._chain) == 10


# ---------------------------------------------------------------------------
# TestFallbackCleanup
# ---------------------------------------------------------------------------


class TestFallbackCleanup:
    @patch("arcllm.modules.fallback.load_model")
    async def test_fallback_adapter_closed_after_success(
        self, mock_load_model, messages
    ):
        inner = _make_inner([_api_error(500)])
        fallback_inner = _make_inner([_FALLBACK_RESPONSE])
        fallback_inner.close = AsyncMock()
        mock_load_model.return_value = fallback_inner

        config = {"chain": ["openai"]}
        module = FallbackModule(config, inner)
        await module.invoke(messages)

        fallback_inner.close.assert_awaited_once()

    @patch("arcllm.modules.fallback.load_model")
    async def test_fallback_adapter_closed_after_failure(
        self, mock_load_model, messages
    ):
        inner = _make_inner([_api_error(500)])
        fallback_inner = _make_inner([_api_error(503)])
        fallback_inner.close = AsyncMock()
        mock_load_model.return_value = fallback_inner

        config = {"chain": ["openai"]}
        module = FallbackModule(config, inner)

        with pytest.raises(ArcLLMAPIError, match="500"):
            await module.invoke(messages)

        fallback_inner.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# TestFallbackLogging
# ---------------------------------------------------------------------------


class TestFallbackLogging:
    @patch("arcllm.modules.fallback.load_model")
    async def test_logs_primary_failure(self, mock_load_model, messages, caplog):
        inner = _make_inner([_api_error(500)])
        fallback_inner = _make_inner([_FALLBACK_RESPONSE])
        mock_load_model.return_value = fallback_inner

        config = {"chain": ["openai"]}
        module = FallbackModule(config, inner)
        with caplog.at_level(logging.WARNING, logger="arcllm.modules.fallback"):
            await module.invoke(messages)
        assert "Primary provider failed" in caplog.text
        assert "1 fallback" in caplog.text

    @patch("arcllm.modules.fallback.load_model")
    async def test_logs_fallback_success(self, mock_load_model, messages, caplog):
        inner = _make_inner([_api_error(500)])
        fallback_inner = _make_inner([_FALLBACK_RESPONSE])
        mock_load_model.return_value = fallback_inner

        config = {"chain": ["openai"]}
        module = FallbackModule(config, inner)
        with caplog.at_level(logging.INFO, logger="arcllm.modules.fallback"):
            await module.invoke(messages)
        assert "Fallback to 'openai' succeeded" in caplog.text

    @patch("arcllm.modules.fallback.load_model")
    async def test_logs_all_fallbacks_exhausted(self, mock_load_model, messages, caplog):
        inner = _make_inner([_api_error(500)])
        fallback_inner = _make_inner([_api_error(503)])
        mock_load_model.return_value = fallback_inner

        config = {"chain": ["openai"]}
        module = FallbackModule(config, inner)
        with caplog.at_level(logging.ERROR, logger="arcllm.modules.fallback"):
            with pytest.raises(ArcLLMAPIError):
                await module.invoke(messages)
        assert "All 1 fallbacks exhausted" in caplog.text

    async def test_no_log_on_primary_success(self, messages, caplog):
        inner = _make_inner([_OK_RESPONSE])
        config = {"chain": ["openai"]}
        module = FallbackModule(config, inner)
        with caplog.at_level(logging.WARNING, logger="arcllm.modules.fallback"):
            await module.invoke(messages)
        assert caplog.text == ""
