"""Tests for RetryModule — exponential backoff with jitter."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from arcllm.exceptions import ArcLLMAPIError, ArcLLMConfigError
from arcllm.modules.retry import RetryModule
from arcllm.types import LLMProvider, LLMResponse, Message, Usage

_OK_RESPONSE = LLMResponse(
    content="ok",
    usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
    model="test-model",
    stop_reason="end_turn",
)


def _make_inner(side_effects):
    """Create a mock inner provider with specified side effects."""
    inner = MagicMock(spec=LLMProvider)
    inner.name = "test-provider"
    inner.model_name = "test-model"
    inner.validate_config.return_value = True
    inner.invoke = AsyncMock(side_effect=side_effects)
    return inner


def _api_error(status_code: int) -> ArcLLMAPIError:
    return ArcLLMAPIError(status_code=status_code, body="error", provider="test")


@pytest.fixture
def messages():
    return [Message(role="user", content="hi")]


@pytest.fixture
def default_config():
    return {
        "max_retries": 3,
        "backoff_base_seconds": 0.01,  # fast for tests
        "max_wait_seconds": 1.0,
        "retryable_status_codes": [429, 500, 502, 503, 529],
    }


# ---------------------------------------------------------------------------
# TestRetrySuccess
# ---------------------------------------------------------------------------


class TestRetrySuccess:
    async def test_first_try_succeeds_no_retry(self, messages, default_config):
        inner = _make_inner([_OK_RESPONSE])
        module = RetryModule(default_config, inner)
        result = await module.invoke(messages)
        assert result.content == "ok"
        assert inner.invoke.await_count == 1

    async def test_retry_on_429_then_succeed(self, messages, default_config):
        inner = _make_inner([_api_error(429), _OK_RESPONSE])
        module = RetryModule(default_config, inner)
        result = await module.invoke(messages)
        assert result.content == "ok"
        assert inner.invoke.await_count == 2

    async def test_retry_on_500_then_succeed(self, messages, default_config):
        inner = _make_inner([_api_error(500), _OK_RESPONSE])
        module = RetryModule(default_config, inner)
        result = await module.invoke(messages)
        assert result.content == "ok"
        assert inner.invoke.await_count == 2

    async def test_retry_on_502_then_succeed(self, messages, default_config):
        inner = _make_inner([_api_error(502), _OK_RESPONSE])
        module = RetryModule(default_config, inner)
        result = await module.invoke(messages)
        assert result.content == "ok"

    async def test_retry_on_503_then_succeed(self, messages, default_config):
        inner = _make_inner([_api_error(503), _OK_RESPONSE])
        module = RetryModule(default_config, inner)
        result = await module.invoke(messages)
        assert result.content == "ok"

    async def test_retry_on_529_then_succeed(self, messages, default_config):
        inner = _make_inner([_api_error(529), _OK_RESPONSE])
        module = RetryModule(default_config, inner)
        result = await module.invoke(messages)
        assert result.content == "ok"

    async def test_retry_on_connection_error(self, messages, default_config):
        inner = _make_inner([httpx.ConnectError("connection refused"), _OK_RESPONSE])
        module = RetryModule(default_config, inner)
        result = await module.invoke(messages)
        assert result.content == "ok"
        assert inner.invoke.await_count == 2

    async def test_retry_on_timeout_error(self, messages, default_config):
        inner = _make_inner([httpx.ReadTimeout("timed out"), _OK_RESPONSE])
        module = RetryModule(default_config, inner)
        result = await module.invoke(messages)
        assert result.content == "ok"
        assert inner.invoke.await_count == 2


# ---------------------------------------------------------------------------
# TestRetryExhaustion
# ---------------------------------------------------------------------------


class TestRetryExhaustion:
    async def test_max_retries_exceeded_raises(self, messages, default_config):
        # max_retries=3 means 4 attempts total (1 initial + 3 retries)
        errors = [_api_error(429)] * 4
        inner = _make_inner(errors)
        module = RetryModule(default_config, inner)
        with pytest.raises(ArcLLMAPIError, match="429"):
            await module.invoke(messages)
        assert inner.invoke.await_count == 4

    async def test_raises_original_error_type(self, messages, default_config):
        inner = _make_inner([httpx.ConnectError("refused")] * 4)
        module = RetryModule(default_config, inner)
        with pytest.raises(httpx.ConnectError):
            await module.invoke(messages)


# ---------------------------------------------------------------------------
# TestRetryPassthrough
# ---------------------------------------------------------------------------


class TestRetryPassthrough:
    async def test_no_retry_on_400(self, messages, default_config):
        inner = _make_inner([_api_error(400)])
        module = RetryModule(default_config, inner)
        with pytest.raises(ArcLLMAPIError, match="400"):
            await module.invoke(messages)
        assert inner.invoke.await_count == 1

    async def test_no_retry_on_401(self, messages, default_config):
        inner = _make_inner([_api_error(401)])
        module = RetryModule(default_config, inner)
        with pytest.raises(ArcLLMAPIError, match="401"):
            await module.invoke(messages)
        assert inner.invoke.await_count == 1

    async def test_no_retry_on_403(self, messages, default_config):
        inner = _make_inner([_api_error(403)])
        module = RetryModule(default_config, inner)
        with pytest.raises(ArcLLMAPIError, match="403"):
            await module.invoke(messages)
        assert inner.invoke.await_count == 1

    async def test_no_retry_on_non_api_error(self, messages, default_config):
        inner = _make_inner([ValueError("bad value")])
        module = RetryModule(default_config, inner)
        with pytest.raises(ValueError, match="bad value"):
            await module.invoke(messages)
        assert inner.invoke.await_count == 1


# ---------------------------------------------------------------------------
# TestRetryBackoff
# ---------------------------------------------------------------------------


class TestRetryBackoff:
    @patch("arcllm.modules.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_backoff_increases_exponentially(
        self, mock_sleep, messages
    ):
        config = {
            "max_retries": 3,
            "backoff_base_seconds": 1.0,
            "max_wait_seconds": 100.0,
            "retryable_status_codes": [429],
        }
        inner = _make_inner([_api_error(429)] * 3 + [_OK_RESPONSE])
        module = RetryModule(config, inner)

        with patch("arcllm.modules.retry.random.uniform", return_value=0.0):
            await module.invoke(messages)

        # Attempts: base*2^0=1, base*2^1=2, base*2^2=4 (jitter=0)
        waits = [call.args[0] for call in mock_sleep.await_args_list]
        assert waits == [1.0, 2.0, 4.0]

    @patch("arcllm.modules.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_backoff_capped_at_max_wait(self, mock_sleep, messages):
        config = {
            "max_retries": 3,
            "backoff_base_seconds": 10.0,
            "max_wait_seconds": 15.0,
            "retryable_status_codes": [500],
        }
        inner = _make_inner([_api_error(500)] * 3 + [_OK_RESPONSE])
        module = RetryModule(config, inner)

        with patch("arcllm.modules.retry.random.uniform", return_value=0.0):
            await module.invoke(messages)

        waits = [call.args[0] for call in mock_sleep.await_args_list]
        # base*2^0=10, base*2^1=20→capped to 15, base*2^2=40→capped to 15
        assert waits == [10.0, 15.0, 15.0]

    @patch("arcllm.modules.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_jitter_proportional_to_backoff(self, mock_sleep, messages):
        """Jitter is uniform(0, backoff) — proportional, not fixed."""
        config = {
            "max_retries": 2,
            "backoff_base_seconds": 1.0,
            "max_wait_seconds": 100.0,
            "retryable_status_codes": [429],
        }
        inner = _make_inner([_api_error(429)] * 2 + [_OK_RESPONSE])
        module = RetryModule(config, inner)

        # Return 50% of the backoff as jitter each time
        with patch("arcllm.modules.retry.random.uniform", return_value=0.5):
            await module.invoke(messages)

        waits = [call.args[0] for call in mock_sleep.await_args_list]
        # attempt 0: backoff=1.0, uniform called with (0, 1.0) returning 0.5 → 1.0+0.5=1.5
        # attempt 1: backoff=2.0, uniform called with (0, 2.0) returning 0.5 → 2.0+0.5=2.5
        assert waits == [1.5, 2.5]


# ---------------------------------------------------------------------------
# TestRetryConfig
# ---------------------------------------------------------------------------


class TestRetryConfig:
    async def test_custom_max_retries(self, messages):
        config = {
            "max_retries": 1,
            "backoff_base_seconds": 0.01,
            "max_wait_seconds": 1.0,
            "retryable_status_codes": [429],
        }
        inner = _make_inner([_api_error(429)] * 3)
        module = RetryModule(config, inner)
        with pytest.raises(ArcLLMAPIError):
            await module.invoke(messages)
        # 1 initial + 1 retry = 2 attempts
        assert inner.invoke.await_count == 2

    async def test_custom_retry_codes(self, messages):
        config = {
            "max_retries": 3,
            "backoff_base_seconds": 0.01,
            "max_wait_seconds": 1.0,
            "retryable_status_codes": [503],  # only 503
        }
        # 429 should NOT be retried with this config
        inner = _make_inner([_api_error(429)])
        module = RetryModule(config, inner)
        with pytest.raises(ArcLLMAPIError, match="429"):
            await module.invoke(messages)
        assert inner.invoke.await_count == 1

        # 503 SHOULD be retried
        inner2 = _make_inner([_api_error(503), _OK_RESPONSE])
        module2 = RetryModule(config, inner2)
        result = await module2.invoke(messages)
        assert result.content == "ok"


# ---------------------------------------------------------------------------
# TestRetryValidation
# ---------------------------------------------------------------------------


class TestRetryValidation:
    def test_negative_max_retries_rejected(self):
        inner = _make_inner([_OK_RESPONSE])
        with pytest.raises(ArcLLMConfigError, match="max_retries must be >= 0"):
            RetryModule({"max_retries": -1}, inner)

    def test_zero_backoff_rejected(self):
        inner = _make_inner([_OK_RESPONSE])
        with pytest.raises(ArcLLMConfigError, match="backoff_base_seconds must be > 0"):
            RetryModule({"backoff_base_seconds": 0}, inner)

    def test_negative_backoff_rejected(self):
        inner = _make_inner([_OK_RESPONSE])
        with pytest.raises(ArcLLMConfigError, match="backoff_base_seconds must be > 0"):
            RetryModule({"backoff_base_seconds": -1.0}, inner)

    def test_zero_max_wait_rejected(self):
        inner = _make_inner([_OK_RESPONSE])
        with pytest.raises(ArcLLMConfigError, match="max_wait_seconds must be > 0"):
            RetryModule({"max_wait_seconds": 0}, inner)

    def test_zero_max_retries_allowed(self):
        """max_retries=0 means 1 attempt only (no retries)."""
        inner = _make_inner([_OK_RESPONSE])
        module = RetryModule({"max_retries": 0}, inner)
        assert module._max_retries == 0


# ---------------------------------------------------------------------------
# TestRetryAfterHeader
# ---------------------------------------------------------------------------


class TestRetryAfterHeader:
    @patch("arcllm.modules.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_retry_after_honored(self, mock_sleep, messages):
        """Retry-After header value is used instead of calculated backoff."""
        config = {
            "max_retries": 1,
            "backoff_base_seconds": 1.0,
            "max_wait_seconds": 100.0,
            "retryable_status_codes": [429],
        }
        error = ArcLLMAPIError(
            status_code=429, body="rate limited", provider="test", retry_after=5.0
        )
        inner = _make_inner([error, _OK_RESPONSE])
        module = RetryModule(config, inner)
        result = await module.invoke(messages)
        assert result.content == "ok"
        mock_sleep.assert_awaited_once_with(5.0)

    @patch("arcllm.modules.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_retry_after_capped_at_max_wait(self, mock_sleep, messages):
        """Retry-After value is capped at max_wait_seconds."""
        config = {
            "max_retries": 1,
            "backoff_base_seconds": 1.0,
            "max_wait_seconds": 3.0,
            "retryable_status_codes": [429],
        }
        error = ArcLLMAPIError(
            status_code=429, body="rate limited", provider="test", retry_after=10.0
        )
        inner = _make_inner([error, _OK_RESPONSE])
        module = RetryModule(config, inner)
        await module.invoke(messages)
        mock_sleep.assert_awaited_once_with(3.0)

    @patch("arcllm.modules.retry.asyncio.sleep", new_callable=AsyncMock)
    async def test_no_retry_after_uses_backoff(self, mock_sleep, messages):
        """Without Retry-After, normal backoff is used."""
        config = {
            "max_retries": 1,
            "backoff_base_seconds": 1.0,
            "max_wait_seconds": 100.0,
            "retryable_status_codes": [429],
        }
        error = ArcLLMAPIError(
            status_code=429, body="rate limited", provider="test", retry_after=None
        )
        inner = _make_inner([error, _OK_RESPONSE])
        module = RetryModule(config, inner)
        with patch("arcllm.modules.retry.random.uniform", return_value=0.0):
            await module.invoke(messages)
        # backoff = 1.0 * 2^0 = 1.0, jitter = 0.0
        mock_sleep.assert_awaited_once_with(1.0)


# ---------------------------------------------------------------------------
# TestRetryLogging
# ---------------------------------------------------------------------------


class TestRetryLogging:
    async def test_logs_retry_attempts(self, messages, default_config, caplog):
        inner = _make_inner([_api_error(429), _OK_RESPONSE])
        module = RetryModule(default_config, inner)
        with caplog.at_level(logging.WARNING, logger="arcllm.modules.retry"):
            await module.invoke(messages)
        assert "Retry attempt 1/3" in caplog.text

    async def test_logs_exhaustion(self, messages, default_config, caplog):
        inner = _make_inner([_api_error(429)] * 4)
        module = RetryModule(default_config, inner)
        with caplog.at_level(logging.ERROR, logger="arcllm.modules.retry"):
            with pytest.raises(ArcLLMAPIError):
                await module.invoke(messages)
        assert "All 3 retries exhausted" in caplog.text

    async def test_no_log_on_success(self, messages, default_config, caplog):
        inner = _make_inner([_OK_RESPONSE])
        module = RetryModule(default_config, inner)
        with caplog.at_level(logging.WARNING, logger="arcllm.modules.retry"):
            await module.invoke(messages)
        assert caplog.text == ""
