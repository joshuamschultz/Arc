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
        # Match the adapter-level httpx timeout (180s) so a long but
        # legitimate LLM call doesn't hit the queue cutoff before the
        # provider one. Configurable per-agent via [modules.queue].
        self._call_timeout: float = config.get("call_timeout", 180.0)
        self._max_queued: int = config.get("max_queued", 10)

        if self._max_concurrent < 1:
            raise ArcLLMConfigError("max_concurrent must be >= 1")
        if self._call_timeout <= 0:
            raise ArcLLMConfigError("call_timeout must be > 0")
        if self._max_queued < 0:
            raise ArcLLMConfigError("max_queued must be >= 0")

        self._semaphore = asyncio.BoundedSemaphore(self._max_concurrent)
        self._waiters: int = 0

        # Observable counters for queue monitoring
        self._total_enqueued: int = 0
        self._total_completed: int = 0
        self._total_rejected: int = 0
        self._total_timeouts: int = 0
        self._active: int = 0
        self._wait_sum_ms: float = 0.0
        self._wait_count: int = 0

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
            self._total_rejected += 1
            self._set_rejected_span_attribute()
            logger.warning(
                "Queue backpressure: %d waiters (max %d)",
                self._waiters,
                self._max_queued,
            )
            raise QueueFullError(self._waiters, self._max_queued)

        self._waiters += 1
        self._total_enqueued += 1
        entry_time = time.monotonic()
        entered_semaphore = False
        try:
            async with self._semaphore:
                entered_semaphore = True
                self._waiters -= 1
                wait_ms = int((time.monotonic() - entry_time) * 1000)
                self._wait_sum_ms += wait_ms
                self._wait_count += 1
                self._active += 1

                self._set_span_attributes(wait_ms, depth_at_entry)

                try:
                    result = await asyncio.wait_for(
                        self._inner.invoke(messages, tools, **kwargs),
                        timeout=self._call_timeout,
                    )
                    self._total_completed += 1
                    return result
                except TimeoutError:
                    self._total_timeouts += 1
                    logger.error(
                        "Queue send-time timeout after %.1fs",
                        self._call_timeout,
                    )
                    raise QueueTimeoutError(self._call_timeout) from None
                finally:
                    self._active -= 1
        except (QueueTimeoutError, QueueFullError):
            raise
        except BaseException:
            if not entered_semaphore:
                # Decrement waiter count if CancelledError hit before
                # we entered the semaphore context (where it's already
                # decremented). Prevents counter drift under cancellation.
                self._waiters -= 1
            raise

    def _set_span_attributes(self, wait_ms: int, depth_at_entry: int) -> None:
        """Set arc.queue.* attributes on the active Otel span."""
        span = trace.get_current_span()
        if span.is_recording():
            span.set_attribute("arc.queue.wait_ms", wait_ms)
            span.set_attribute("arc.queue.depth", depth_at_entry)
            span.set_attribute("arc.queue.call_timeout_ms", int(self._call_timeout * 1000))

    def queue_stats(self) -> dict[str, Any]:
        """Return current queue state for REST API and UI display."""
        avg_wait_ms = (
            round(self._wait_sum_ms / self._wait_count, 1) if self._wait_count > 0 else 0.0
        )
        return {
            "max_concurrent": self._max_concurrent,
            "max_queued": self._max_queued,
            "call_timeout_s": self._call_timeout,
            "active": self._active,
            "waiting": self._waiters,
            "total_enqueued": self._total_enqueued,
            "total_completed": self._total_completed,
            "total_rejected": self._total_rejected,
            "total_timeouts": self._total_timeouts,
            "avg_wait_ms": avg_wait_ms,
        }

    def _set_rejected_span_attribute(self) -> None:
        """Mark the active span as a rejected queue entry."""
        span = trace.get_current_span()
        if span.is_recording():
            span.set_attribute("arc.queue.rejected", True)
