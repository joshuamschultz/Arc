"""RateLimitModule — token bucket rate limiting with per-provider shared state."""

import asyncio
import logging
import time
from typing import Any

from arcllm.exceptions import ArcLLMConfigError
from arcllm.modules.base import BaseModule
from arcllm.types import LLMProvider, LLMResponse, Message, Tool

logger = logging.getLogger(__name__)


class TokenBucket:
    """Token bucket algorithm for rate limiting.

    Starts full at *capacity* tokens. Each ``acquire()`` consumes one token.
    Tokens refill at *refill_rate* per second, capped at *capacity*.
    """

    def __init__(self, capacity: int, refill_rate: float) -> None:
        self._capacity = capacity
        self._tokens: float = float(capacity)
        self._refill_rate = refill_rate
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        """Add tokens based on elapsed time, capped at capacity."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_rate)
        self._last_refill = now

    async def acquire(self) -> float:
        """Consume one token, waiting if the bucket is empty.

        Returns the wait time in seconds (0.0 if a token was immediately
        available).
        """
        total_wait = 0.0
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return total_wait
                # Calculate wait for next token
                deficit = 1.0 - self._tokens
                wait_seconds = deficit / self._refill_rate

            # Sleep *outside* the lock so other callers aren't serialised.
            # If another waiter consumed our token, the loop re-checks.
            await asyncio.sleep(wait_seconds)
            total_wait += wait_seconds


# ---------------------------------------------------------------------------
# Per-provider shared bucket registry
# ---------------------------------------------------------------------------

_bucket_registry: dict[str, TokenBucket] = {}


def _get_or_create_bucket(
    provider: str, capacity: int, refill_rate: float
) -> TokenBucket:
    """Return the shared bucket for *provider*, creating one if needed."""
    if provider not in _bucket_registry:
        _bucket_registry[provider] = TokenBucket(capacity, refill_rate)
    return _bucket_registry[provider]


def clear_buckets() -> None:
    """Remove all shared buckets (for test isolation and cache resets)."""
    _bucket_registry.clear()


# ---------------------------------------------------------------------------
# RateLimitModule
# ---------------------------------------------------------------------------


class RateLimitModule(BaseModule):
    """Acquires a token from a per-provider bucket before each invoke().

    Config keys:
        requests_per_minute: Sustained request rate (required, > 0).
        burst_capacity: Maximum bucket size / burst allowance
                        (default: requests_per_minute).
    """

    def __init__(self, config: dict[str, Any], inner: LLMProvider) -> None:
        super().__init__(config, inner)

        rpm: int = config.get("requests_per_minute", 60)
        if rpm <= 0:
            raise ArcLLMConfigError("requests_per_minute must be > 0")

        capacity: int = config.get("burst_capacity", rpm)
        if capacity < 1:
            raise ArcLLMConfigError("burst_capacity must be >= 1")

        self._provider_name: str = inner.name
        self._bucket: TokenBucket = _get_or_create_bucket(
            self._provider_name, capacity, rpm / 60.0
        )

    async def invoke(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        with self._span("arcllm.rate_limit") as rl_span:
            wait = await self._bucket.acquire()
            wait_ms = round(wait * 1000, 1)
            rl_span.set_attribute("arcllm.rate_limit.wait_ms", wait_ms)
            if wait > 0:
                rl_span.add_event(
                    "throttled",
                    {"arcllm.rate_limit.wait_ms": wait_ms},
                )
                logger.warning(
                    "Rate limited for provider '%s'. Waited %.2fs for token.",
                    self._provider_name,
                    wait,
                )
            return await self._inner.invoke(messages, tools, **kwargs)
