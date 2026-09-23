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
import math
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from opentelemetry import trace

from arcllm.exceptions import ArcLLMConfigError, QueueFullError, QueueTimeoutError
from arcllm.modules.base import BaseModule, owned_stream, validate_config_keys
from arcllm.queue_control import CallJob, CallQueueContext, CallQueueCoordinator
from arcllm.types import Delta, LLMProvider, LLMResponse, Message, Tool

logger = logging.getLogger(__name__)

_VALID_KEYS: set[str] = {
    "enabled",
    "max_concurrent",
    "call_timeout",
    "max_queued",
    "provider_scopes",
}


class QueueModule(BaseModule):
    """Concurrency-limiting wrapper with backpressure and send-time timeout.

    Wraps an inner ``LLMProvider``.  Callers interact with the same
    ``invoke()`` API — the queue is transparent.

    Config keys:
        max_concurrent: Semaphore capacity (default: 2).
        call_timeout:   Send-time timeout in seconds (default: 180.0).
        max_queued:     Max waiters before rejection (default: 10).
    """

    def __init__(
        self,
        config: dict[str, Any],
        inner: LLMProvider,
        *,
        coordinator: CallQueueCoordinator | None = None,
        default_context: CallQueueContext | None = None,
    ) -> None:
        validate_config_keys(config, _VALID_KEYS, "queue")
        super().__init__(config, inner)

        self._max_concurrent: int = config.get("max_concurrent", 2)
        # Match the adapter-level httpx timeout (180s) so a long but
        # legitimate LLM call doesn't hit the queue cutoff before the
        # provider one. Configurable per-agent via [modules.queue].
        self._call_timeout: float = config.get("call_timeout", 180.0)
        self._max_queued: int = config.get("max_queued", 10)

        if type(self._max_concurrent) is not int:
            raise ArcLLMConfigError("max_concurrent must be an integer")
        if self._max_concurrent < 1:
            raise ArcLLMConfigError("max_concurrent must be >= 1")
        if type(self._max_queued) is not int:
            raise ArcLLMConfigError("max_queued must be an integer")
        if self._max_queued < 0:
            raise ArcLLMConfigError("max_queued must be >= 0")
        if type(self._call_timeout) not in (int, float) or not math.isfinite(self._call_timeout):
            raise ArcLLMConfigError("call_timeout must be a finite positive number")
        if self._call_timeout <= 0:
            raise ArcLLMConfigError("call_timeout must be > 0")

        self._semaphore = asyncio.BoundedSemaphore(self._max_concurrent)
        self._coordinator = coordinator
        self._default_context = default_context
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
        if self._coordinator is not None:
            context = self._resolve_context(kwargs.pop("queue_context", None))
            async with self._coordinator.call(
                context, execution_timeout=self._call_timeout
            ) as job:
                with self._coordinator.provider_task(job):
                    return await self._inner.invoke(
                        messages, tools, _queue_job=job, _queue_context=context, **kwargs
                    )
        async with self._admit():
            budget = asyncio.timeout(self._call_timeout)
            try:
                async with budget:
                    result = await self._inner.invoke(messages, tools, **kwargs)
            except TimeoutError:
                if not budget.expired():
                    raise
                self._record_timeout()
                raise QueueTimeoutError(self._call_timeout) from None
            self._total_completed += 1
            return result

    async def invoke_stream(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[Delta]:
        """Hold one queue slot until the provider stream ends or is closed."""
        if self._coordinator is not None:
            context = self._resolve_context(kwargs.pop("queue_context", None))
            async with self._coordinator.call(
                context, execution_timeout=self._call_timeout
            ) as job:
                delegated = self._inner.invoke_stream(
                    messages, tools, _queue_job=job, _queue_context=context, **kwargs
                )
                self._coordinator.bind_stream(job.call_id, delegated)
                async with owned_stream(delegated) as stream:
                    while True:
                        try:
                            with self._coordinator.provider_task(job):
                                delta = await anext(stream)
                        except StopAsyncIteration:
                            break
                        yield delta
            return
        async with self._admit():
            deadline = time.monotonic() + self._call_timeout
            async with owned_stream(
                self._inner.invoke_stream(messages, tools, **kwargs)
            ) as stream:
                while True:
                    try:
                        delta = await self._next_delta(stream, deadline)
                    except StopAsyncIteration:
                        break
                    yield delta
            self._total_completed += 1

    def _resolve_context(self, requested: object) -> CallQueueContext:
        """Keep a bound run's identity authoritative over call arguments."""
        coordinator = self._coordinator
        if coordinator is None:
            raise RuntimeError("shared queue coordinator is unavailable")
        bound = coordinator.current_context
        if bound is not None and requested is not None and requested != bound:
            raise ValueError("queue context conflicts with the bound run")
        context = bound or requested or self._default_context
        if not isinstance(context, CallQueueContext):
            raise ValueError("a trusted queue context is required")
        return context

    async def _next_delta(self, stream: AsyncIterator[Delta], deadline: float) -> Delta:
        """Bound only the provider await, leaving consumer work uncancelled."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            self._record_timeout()
            raise QueueTimeoutError(self._call_timeout)
        budget = asyncio.timeout(remaining)
        try:
            async with budget:
                return await anext(stream)
        except TimeoutError:
            if not budget.expired():
                raise
            self._record_timeout()
            raise QueueTimeoutError(self._call_timeout) from None

    @asynccontextmanager
    async def _admit(self) -> AsyncIterator[None]:
        """Share admission, accounting and cleanup across both call shapes."""
        depth_at_entry = self._waiters

        # Backpressure: reject immediately if too many callers are waiting
        if self._semaphore.locked() and self._waiters >= self._max_queued:
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
                    yield
                finally:
                    self._active -= 1
        except BaseException:
            if not entered_semaphore:
                # Decrement waiter count if CancelledError hit before
                # we entered the semaphore context (where it's already
                # decremented). Prevents counter drift under cancellation.
                self._waiters -= 1
            raise

    def _record_timeout(self) -> None:
        self._total_timeouts += 1
        logger.error("Queue send-time timeout after %.1fs", self._call_timeout)

    def _set_span_attributes(self, wait_ms: int, depth_at_entry: int) -> None:
        """Set arc.queue.* attributes on the active Otel span."""
        span = trace.get_current_span()
        if span.is_recording():
            span.set_attribute("arc.queue.wait_ms", wait_ms)
            span.set_attribute("arc.queue.depth", depth_at_entry)
            span.set_attribute("arc.queue.call_timeout_ms", int(self._call_timeout * 1000))

    def queue_stats(self) -> dict[str, Any]:
        """Return current queue state for REST API and UI display."""
        if self._coordinator is not None:
            return {**self._coordinator.snapshot(), "call_timeout_s": self._call_timeout}
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


class ProviderQueueModule(BaseModule):
    """Admit only the selected wire adapter, never the router or fallback stack."""

    def __init__(
        self, inner: LLMProvider, coordinator: CallQueueCoordinator, provider_scope: str
    ) -> None:
        super().__init__({}, inner)
        self._coordinator = coordinator
        self._provider_scope = provider_scope

    async def invoke(
        self, messages: list[Message], tools: list[Tool] | None = None, **kwargs: Any
    ) -> LLMResponse:
        """Hold provider capacity for exactly one wire request."""
        job = kwargs.pop("_queue_job", None)
        if not isinstance(job, CallJob):
            raise RuntimeError("provider attempt has no queue owner")
        async with self._coordinator.attempt(self._provider_scope, job):
            budget = asyncio.timeout(self._coordinator.remaining_execution(job))
            try:
                async with budget:
                    return await self._inner.invoke(messages, tools, **kwargs)
            except TimeoutError:
                if not budget.expired():
                    raise
                raise QueueTimeoutError(self._coordinator.execution_timeout(job)) from None

    async def invoke_stream(
        self, messages: list[Message], tools: list[Tool] | None = None, **kwargs: Any
    ) -> AsyncIterator[Delta]:
        """Retain capacity until the actual provider iterator closes."""
        job = kwargs.pop("_queue_job", None)
        if not isinstance(job, CallJob):
            raise RuntimeError("provider attempt has no queue owner")
        async with self._coordinator.attempt(self._provider_scope, job):
            async with owned_stream(
                self._inner.invoke_stream(messages, tools, **kwargs)
            ) as stream:
                while True:
                    budget = asyncio.timeout(self._coordinator.remaining_execution(job))
                    try:
                        async with budget:
                            delta = await anext(stream)
                    except StopAsyncIteration:
                        break
                    except TimeoutError:
                        if not budget.expired():
                            raise
                        raise QueueTimeoutError(self._coordinator.execution_timeout(job)) from None
                    yield delta
