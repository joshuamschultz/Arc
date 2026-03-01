"""QueueModule — bounded concurrency with backpressure for LLM calls.

Gates ``invoke()`` through an ``asyncio.BoundedSemaphore`` so that at most
``max_concurrent`` calls hit the provider simultaneously.  Excess callers
wait in FIFO order.  When the wait queue itself exceeds ``max_queued``,
new calls are immediately rejected with ``QueueFullError``.

Timeouts are *send-time only*: the clock starts after the semaphore is
acquired, measuring actual provider response time — not invisible queue
wait.

Stack position: Otel → **Queue** → Telemetry → Audit → …
"""

import asyncio
import logging
import time
from typing import Any

from opentelemetry import trace

from arcllm.exceptions import ArcLLMConfigError, QueueFullError, QueueTimeoutError
from arcllm.modules.base import BaseModule, validate_config_keys
from arcllm.types import LLMProvider, LLMResponse, Message, Tool

logger = logging.getLogger(__name__)

_VALID_KEYS: set[str] = {"enabled", "max_concurrent", "call_timeout", "max_queued"}


class QueueModule(BaseModule):
    """Concurrency-limiting wrapper with backpressure and send-time timeout.

    Wraps an inner ``LLMProvider``.  Callers interact with the same
    ``invoke()`` API — the queue is transparent.

    Config keys:
        max_concurrent: Semaphore capacity (default: 2).
        call_timeout:   Send-time timeout in seconds (default: 60.0).
        max_queued:     Max waiters before rejection (default: 10).
    """

    def __init__(self, config: dict[str, Any], inner: LLMProvider) -> None:
        validate_config_keys(config, _VALID_KEYS, "queue")
        super().__init__(config, inner)

        self._max_concurrent: int = config.get("max_concurrent", 2)
        self._call_timeout: float = config.get("call_timeout", 60.0)
        self._max_queued: int = config.get("max_queued", 10)

        if self._max_concurrent < 1:
            raise ArcLLMConfigError("max_concurrent must be >= 1")
        if self._call_timeout <= 0:
            raise ArcLLMConfigError("call_timeout must be > 0")
        if self._max_queued < 0:
            raise ArcLLMConfigError("max_queued must be >= 0")

        self._semaphore = asyncio.BoundedSemaphore(self._max_concurrent)
        self._waiters: int = 0

    async def invoke(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Gate the inner invoke() through the concurrency semaphore."""
        depth_at_entry = self._waiters

        # Backpressure: reject immediately if too many callers are waiting
        if self._waiters >= self._max_queued:
            self._set_rejected_span_attribute()
            logger.warning(
                "Queue backpressure: %d waiters (max %d)",
                self._waiters,
                self._max_queued,
            )
            raise QueueFullError(self._waiters, self._max_queued)

        self._waiters += 1
        entry_time = time.monotonic()
        try:
            async with self._semaphore:
                self._waiters -= 1
                wait_ms = int((time.monotonic() - entry_time) * 1000)

                self._set_span_attributes(wait_ms, depth_at_entry)

                try:
                    return await asyncio.wait_for(
                        self._inner.invoke(messages, tools, **kwargs),
                        timeout=self._call_timeout,
                    )
                except TimeoutError:
                    logger.error(
                        "Queue send-time timeout after %.1fs",
                        self._call_timeout,
                    )
                    raise QueueTimeoutError(self._call_timeout) from None
        except (QueueTimeoutError, QueueFullError):
            raise
        except BaseException:
            # Ensure waiter count is decremented if we never entered the
            # semaphore context (e.g. CancelledError while waiting).
            # Inside `async with` the decrement already happened.
            # BoundedSemaphore context manager handles release.
            raise

    def _set_span_attributes(self, wait_ms: int, depth_at_entry: int) -> None:
        """Set arc.queue.* attributes on the active Otel span."""
        span = trace.get_current_span()
        if span.is_recording():
            span.set_attribute("arc.queue.wait_ms", wait_ms)
            span.set_attribute("arc.queue.depth", depth_at_entry)
            span.set_attribute("arc.queue.call_timeout_ms", int(self._call_timeout * 1000))

    def _set_rejected_span_attribute(self) -> None:
        """Mark the active span as a rejected queue entry."""
        span = trace.get_current_span()
        if span.is_recording():
            span.set_attribute("arc.queue.rejected", True)
