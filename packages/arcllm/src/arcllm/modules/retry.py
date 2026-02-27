"""RetryModule — exponential backoff with jitter on transient failures."""

import asyncio
import logging
import random
from typing import Any

import httpx

from arcllm.exceptions import ArcLLMAPIError, ArcLLMConfigError
from arcllm.modules.base import BaseModule
from arcllm.types import LLMProvider, LLMResponse, Message, Tool

logger = logging.getLogger(__name__)

# Default retryable HTTP status codes.
_DEFAULT_RETRYABLE_CODES = [429, 500, 502, 503, 529]


class RetryModule(BaseModule):
    """Retries transient failures with exponential backoff + jitter.

    Wraps an inner LLMProvider. On retryable errors (specific HTTP status
    codes or connection-level failures), waits with exponential backoff
    and retries up to max_retries times.

    Rate limits (429) get special treatment: more retries and the
    Retry-After header is honored without capping, since rate limits
    are guaranteed to resolve — they just need patience.

    Config keys:
        max_retries: Maximum retry attempts after initial try (default: 3).
        rate_limit_max_retries: Max retries specifically for 429s (default: 6).
        backoff_base_seconds: Base wait time in seconds (default: 1.0).
        max_wait_seconds: Maximum wait time cap (default: 60.0).
        retryable_status_codes: HTTP codes to retry (default: [429,500,502,503,529]).
    """

    def __init__(self, config: dict[str, Any], inner: LLMProvider) -> None:
        super().__init__(config, inner)
        self._max_retries: int = config.get("max_retries", 3)
        self._rate_limit_max_retries: int = config.get("rate_limit_max_retries", 6)
        self._backoff_base: float = config.get("backoff_base_seconds", 1.0)
        self._max_wait: float = config.get("max_wait_seconds", 60.0)
        self._retryable_codes: set[int] = set(
            config.get("retryable_status_codes", _DEFAULT_RETRYABLE_CODES)
        )
        # Validate config bounds
        if self._max_retries < 0:
            raise ArcLLMConfigError("max_retries must be >= 0")
        if self._rate_limit_max_retries < 0:
            raise ArcLLMConfigError("rate_limit_max_retries must be >= 0")
        if self._backoff_base <= 0:
            raise ArcLLMConfigError("backoff_base_seconds must be > 0")
        if self._max_wait <= 0:
            raise ArcLLMConfigError("max_wait_seconds must be > 0")

    async def invoke(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        from opentelemetry.trace import StatusCode

        last_error: Exception | None = None
        effective_max = self._max_retries

        with self._span("arcllm.retry") as retry_span:
            for attempt in range(self._rate_limit_max_retries + 1):
                attrs = {"arcllm.retry.attempt": attempt}
                with self._span("arcllm.retry.attempt", attributes=attrs) as attempt_span:
                    try:
                        return await self._inner.invoke(messages, tools, **kwargs)
                    except (ArcLLMAPIError, httpx.ConnectError, httpx.TimeoutException) as e:
                        if not self._is_retryable(e):
                            raise
                        last_error = e
                        attempt_span.record_exception(e)
                        # Use higher retry budget for rate limits (429)
                        effective_max = self._effective_max_retries(e)
                        if attempt < effective_max:
                            wait = self._calculate_wait(attempt, e)
                            logger.warning(
                                "Retry attempt %d/%d after %.2fs: %s",
                                attempt + 1,
                                effective_max,
                                wait,
                                e,
                            )
                            await asyncio.sleep(wait)
                        else:
                            break

            logger.error("All %d retries exhausted: %s", effective_max, last_error)
            retry_span.set_status(StatusCode.ERROR)
            raise last_error  # type: ignore[misc]

    def _effective_max_retries(self, error: Exception) -> int:
        """Return retry budget based on error type.

        Rate limits (429) get a higher budget since they're guaranteed
        to resolve — the provider is just asking us to wait.
        """
        if isinstance(error, ArcLLMAPIError) and error.status_code == 429:
            return self._rate_limit_max_retries
        return self._max_retries

    def _is_retryable(self, error: Exception) -> bool:
        """Check if an error is retryable."""
        if isinstance(error, ArcLLMAPIError):
            return error.status_code in self._retryable_codes
        if isinstance(error, (httpx.ConnectError, httpx.TimeoutException)):
            return True
        return False

    def _calculate_wait(self, attempt: int, error: Exception | None = None) -> float:
        """Calculate wait time with exponential backoff + proportional jitter.

        Honors Retry-After header from ArcLLMAPIError when present.
        For rate limits (429), the retry-after is not capped — the
        provider knows exactly when capacity will be available.
        For other errors, retry-after is capped at max_wait_seconds.
        """
        if isinstance(error, ArcLLMAPIError) and error.retry_after is not None:
            # Rate limits: trust the provider's retry-after without capping
            if error.status_code == 429:
                return error.retry_after
            return min(error.retry_after, self._max_wait)
        backoff = self._backoff_base * (2**attempt)
        jitter = random.uniform(0, backoff)  # noqa: S311 — non-cryptographic jitter
        return min(backoff + jitter, self._max_wait)
