"""Tests for RateLimitModule — token bucket rate limiting."""

import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcllm.exceptions import ArcLLMConfigError
from arcllm.modules.rate_limit import (
    RateLimitModule,
    TokenBucket,
    _bucket_registry,
    clear_buckets,
)
from arcllm.types import LLMProvider, LLMResponse, Message, Usage

_OK_RESPONSE = LLMResponse(
    content="ok",
    usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
    model="test-model",
    stop_reason="end_turn",
)


def _make_inner(name: str = "test-provider"):
    inner = MagicMock(spec=LLMProvider)
    inner.name = name
    inner.model_name = "test-model"
    inner.validate_config.return_value = True
    inner.invoke = AsyncMock(return_value=_OK_RESPONSE)
    return inner


def _freeze_then_advance(start: float, step: float = 1.0):
    """Mock side_effect for time.monotonic(): freeze on first call, then advance.

    Returns *start* on the first call (simulating zero elapsed time during the
    initial refill check inside the lock), then advances by *step* on each
    subsequent call so the post-sleep refill sees enough elapsed time.
    """
    state = {"calls": 0, "t": start}

    def _side_effect():
        state["calls"] += 1
        if state["calls"] <= 1:
            return state["t"]
        state["t"] += step
        return state["t"]

    return _side_effect


@pytest.fixture
def messages():
    return [Message(role="user", content="hi")]


@pytest.fixture(autouse=True)
def _clean_buckets():
    """Clear shared bucket registry between tests."""
    clear_buckets()
    yield
    clear_buckets()


# ---------------------------------------------------------------------------
# TestTokenBucket
# ---------------------------------------------------------------------------


class TestTokenBucket:
    async def test_starts_with_full_capacity(self):
        bucket = TokenBucket(capacity=10, refill_rate=1.0)
        assert bucket._tokens == 10

    async def test_acquire_consumes_token(self):
        bucket = TokenBucket(capacity=10, refill_rate=1.0)
        wait = await bucket.acquire()
        assert wait == 0.0
        assert bucket._tokens == 9.0

    @patch("arcllm.modules.rate_limit.asyncio.sleep", new_callable=AsyncMock)
    @patch("arcllm.modules.rate_limit.time.monotonic")
    async def test_acquire_when_empty_waits(self, mock_mono, mock_sleep):
        t = 1000.0
        mock_mono.return_value = t
        bucket = TokenBucket(capacity=1, refill_rate=1.0)

        # Consume the only token
        await bucket.acquire()

        # First refill inside lock sees no elapsed time (still t=1000).
        # After sleep, the re-acquire refill advances by 1s → token available.
        mock_mono.side_effect = _freeze_then_advance(t)
        wait = await bucket.acquire()
        assert wait > 0
        mock_sleep.assert_awaited_once()

    async def test_acquire_returns_zero_when_immediate(self):
        bucket = TokenBucket(capacity=5, refill_rate=1.0)
        wait = await bucket.acquire()
        assert wait == 0.0

    @patch("arcllm.modules.rate_limit.time.monotonic")
    async def test_refill_adds_tokens_over_time(self, mock_mono):
        mock_mono.return_value = 1000.0
        bucket = TokenBucket(capacity=10, refill_rate=2.0)
        # Consume all tokens
        for _ in range(10):
            await bucket.acquire()
        assert bucket._tokens == 0.0

        # Advance time by 3 seconds → should refill 6 tokens (2/sec * 3s)
        mock_mono.return_value = 1003.0
        bucket._refill()
        assert bucket._tokens == 6.0

    @patch("arcllm.modules.rate_limit.time.monotonic")
    async def test_refill_capped_at_capacity(self, mock_mono):
        mock_mono.return_value = 1000.0
        bucket = TokenBucket(capacity=5, refill_rate=10.0)

        # Advance time by 100 seconds → would refill 1000 tokens, but capped at 5
        mock_mono.return_value = 1100.0
        bucket._refill()
        assert bucket._tokens == 5

    async def test_burst_allows_multiple_immediate(self):
        bucket = TokenBucket(capacity=5, refill_rate=1.0)
        waits = []
        for _ in range(5):
            w = await bucket.acquire()
            waits.append(w)
        assert all(w == 0.0 for w in waits)

    @patch("arcllm.modules.rate_limit.asyncio.sleep", new_callable=AsyncMock)
    @patch("arcllm.modules.rate_limit.time.monotonic")
    async def test_burst_exhausted_then_waits(self, mock_mono, mock_sleep):
        t = 1000.0
        mock_mono.return_value = t

        bucket = TokenBucket(capacity=2, refill_rate=1.0)
        # Consume both tokens
        await bucket.acquire()
        await bucket.acquire()

        # First refill sees no elapsed time; after sleep, advance 1s.
        mock_mono.side_effect = _freeze_then_advance(t)
        wait = await bucket.acquire()
        assert wait > 0
        mock_sleep.assert_awaited_once()


# ---------------------------------------------------------------------------
# TestRateLimitModule
# ---------------------------------------------------------------------------


class TestRateLimitModule:
    async def test_invoke_delegates_to_inner(self, messages):
        inner = _make_inner()
        config = {"requests_per_minute": 60}
        module = RateLimitModule(config, inner)
        result = await module.invoke(messages)
        inner.invoke.assert_awaited_once_with(messages, None)
        assert result.content == "ok"

    async def test_invoke_passes_tools_and_kwargs(self, messages):
        inner = _make_inner()
        tools = [MagicMock()]
        config = {"requests_per_minute": 60}
        module = RateLimitModule(config, inner)
        await module.invoke(messages, tools=tools, max_tokens=100)
        inner.invoke.assert_awaited_once_with(messages, tools, max_tokens=100)

    @patch("arcllm.modules.rate_limit.asyncio.sleep", new_callable=AsyncMock)
    @patch("arcllm.modules.rate_limit.time.monotonic")
    async def test_logs_warning_when_throttled(
        self, mock_mono, mock_sleep, messages, caplog
    ):
        t = 1000.0
        mock_mono.return_value = t
        inner = _make_inner("anthropic")
        config = {"requests_per_minute": 60, "burst_capacity": 1}
        module = RateLimitModule(config, inner)

        # First call uses the one token
        await module.invoke(messages)

        # First refill sees no elapsed time; after sleep, advance.
        mock_mono.side_effect = _freeze_then_advance(t)

        # Second call should throttle and log
        with caplog.at_level(logging.WARNING, logger="arcllm.modules.rate_limit"):
            await module.invoke(messages)

        assert "Rate limited" in caplog.text
        assert "anthropic" in caplog.text

    async def test_no_log_when_immediate(self, messages, caplog):
        inner = _make_inner()
        config = {"requests_per_minute": 60}
        module = RateLimitModule(config, inner)
        with caplog.at_level(logging.WARNING, logger="arcllm.modules.rate_limit"):
            await module.invoke(messages)
        assert caplog.text == ""

    async def test_provider_name_from_inner(self):
        inner = _make_inner("my-provider")
        config = {"requests_per_minute": 60}
        module = RateLimitModule(config, inner)
        assert module._provider_name == "my-provider"


# ---------------------------------------------------------------------------
# TestRateLimitValidation
# ---------------------------------------------------------------------------


class TestRateLimitValidation:
    def test_zero_rpm_rejected(self):
        inner = _make_inner()
        with pytest.raises(ArcLLMConfigError, match="requests_per_minute must be > 0"):
            RateLimitModule({"requests_per_minute": 0}, inner)

    def test_negative_rpm_rejected(self):
        inner = _make_inner()
        with pytest.raises(ArcLLMConfigError, match="requests_per_minute must be > 0"):
            RateLimitModule({"requests_per_minute": -10}, inner)

    def test_zero_burst_rejected(self):
        inner = _make_inner()
        with pytest.raises(
            ArcLLMConfigError, match="burst_capacity must be >= 1"
        ):
            RateLimitModule(
                {"requests_per_minute": 60, "burst_capacity": 0}, inner
            )

    def test_burst_defaults_to_rpm(self):
        inner = _make_inner()
        module = RateLimitModule({"requests_per_minute": 120}, inner)
        assert module._bucket._capacity == 120


# ---------------------------------------------------------------------------
# TestBucketRegistry
# ---------------------------------------------------------------------------


class TestBucketRegistry:
    def test_same_provider_shares_bucket(self):
        inner1 = _make_inner("anthropic")
        inner2 = _make_inner("anthropic")
        config = {"requests_per_minute": 60}
        m1 = RateLimitModule(config, inner1)
        m2 = RateLimitModule(config, inner2)
        assert m1._bucket is m2._bucket

    def test_different_providers_different_buckets(self):
        inner1 = _make_inner("anthropic")
        inner2 = _make_inner("openai")
        config = {"requests_per_minute": 60}
        m1 = RateLimitModule(config, inner1)
        m2 = RateLimitModule(config, inner2)
        assert m1._bucket is not m2._bucket

    def test_clear_buckets_removes_all(self):
        inner = _make_inner("anthropic")
        config = {"requests_per_minute": 60}
        RateLimitModule(config, inner)
        assert "anthropic" in _bucket_registry
        clear_buckets()
        assert len(_bucket_registry) == 0


# ---------------------------------------------------------------------------
# TestConcurrentAccess
# ---------------------------------------------------------------------------


class TestConcurrentAccess:
    async def test_concurrent_acquires_never_go_negative(self):
        """Multiple concurrent acquires must never allow tokens below 0."""
        bucket = TokenBucket(capacity=3, refill_rate=100.0)
        # Drain all tokens first
        for _ in range(3):
            await bucket.acquire()

        # Launch 5 concurrent acquires — only 1-2 tokens will refill quickly.
        # The loop-based acquire should re-check and wait, never going negative.
        results = await asyncio.gather(
            bucket.acquire(),
            bucket.acquire(),
            bucket.acquire(),
            bucket.acquire(),
            bucket.acquire(),
        )
        # All should return (some waited, some didn't), tokens must be >= 0
        assert bucket._tokens >= 0.0
        assert len(results) == 5
        assert all(isinstance(w, float) for w in results)
