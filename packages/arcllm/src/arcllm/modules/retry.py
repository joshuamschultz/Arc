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

    Config keys:
        max_retries: Maximum retry attempts after initial try (default: 3).
        backoff_base_seconds: Base wait time in seconds (default: 1.0).
        max_wait_seconds: Maximum wait time cap (default: 60.0).
        retryable_status_codes: HTTP codes to retry (default: [429,500,502,503,529]).
    """

    def __init__(self, config: dict[str, Any], inner: LLMProvider) -> None:
        super().__init__(config, inner)
        self._max_retries: int = config.get("max_retries", 3)
        self._backoff_base: float = config.get("backoff_base_seconds", 1.0)
        self._max_wait: float = config.get("max_wait_seconds", 60.0)
        self._retryable_codes: set[int] = set(
            config.get("retryable_status_codes", _DEFAULT_RETRYABLE_CODES)
        )
        # Validate config bounds
        if self._max_retries < 0:
            raise ArcLLMConfigError("max_retries must be >= 0")
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

        with self._span("arcllm.retry") as retry_span:
            for attempt in range(self._max_retries + 1):
                with self._span("arcllm.retry.attempt", attributes={"arcllm.retry.attempt": attempt}) as attempt_span:
                    try:
                        return await self._inner.invoke(messages, tools, **kwargs)
                    except (ArcLLMAPIError, httpx.ConnectError, httpx.TimeoutException) as e:
                        if not self._is_retryable(e):
                            raise
                        last_error = e
                        attempt_span.record_exception(e)
                        attempt_span.set_status(StatusCode.OK)
                        if attempt < self._max_retries:
                            wait = self._calculate_wait(attempt, e)
                            logger.warning(
                                "Retry attempt %d/%d after %.2fs: %s",
                                attempt + 1,
                                self._max_retries,
                                wait,
                                e,
                            )
                            await asyncio.sleep(wait)

            logger.error("All %d retries exhausted: %s", self._max_retries, last_error)
            retry_span.set_status(StatusCode.ERROR)
            raise last_error  # type: ignore[misc]

    def _is_retryable(self, error: Exception) -> bool:
        """Check if an error is retryable."""
        if isinstance(error, ArcLLMAPIError):
            return error.status_code in self._retryable_codes
        if isinstance(error, (httpx.ConnectError, httpx.TimeoutException)):
            return True
        return False

    def _calculate_wait(self, attempt: int, error: Exception | None = None) -> float:
        """Calculate wait time with exponential backoff + proportional jitter.

        Honors Retry-After header from ArcLLMAPIError when present,
        capped at max_wait_seconds.
        """
        # Honor Retry-After header if present
        if isinstance(error, ArcLLMAPIError) and error.retry_after is not None:
            return min(error.retry_after, self._max_wait)
        backoff = self._backoff_base * (2**attempt)
        jitter = random.uniform(0, backoff)
        return min(backoff + jitter, self._max_wait)
