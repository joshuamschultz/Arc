"""Tests for QueueModule — bounded concurrency with backpressure."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcllm.exceptions import ArcLLMConfigError, QueueFullError, QueueTimeoutError
from arcllm.modules.queue import QueueModule
from arcllm.types import Delta, LLMProvider, LLMResponse, Message, Usage

_OK_RESPONSE = LLMResponse(
    content="ok",
    usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
    model="test-model",
    stop_reason="end_turn",
)


def _make_inner(
    side_effect: list | None = None,
    delay: float = 0.0,
) -> MagicMock:
    """Create a mock inner provider, optionally with a per-call delay."""
    inner = MagicMock(spec=LLMProvider)
    inner.name = "test-provider"
    inner.model_name = "test-model"
    inner.validate_config.return_value = True

    if delay > 0:

        async def _slow_invoke(*_args, **_kwargs):
            await asyncio.sleep(delay)
            return _OK_RESPONSE

        inner.invoke = AsyncMock(side_effect=_slow_invoke)
    elif side_effect is not None:
        inner.invoke = AsyncMock(side_effect=side_effect)
    else:
        inner.invoke = AsyncMock(return_value=_OK_RESPONSE)
    return inner


@pytest.fixture
def messages():
    return [Message(role="user", content="hi")]


# ---------------------------------------------------------------------------
# TestQueueConcurrency
# ---------------------------------------------------------------------------


class TestQueueConcurrency:
    """R-001: Concurrency limiting via BoundedSemaphore."""

    async def test_invoke_succeeds_within_concurrency_limit(self, messages):
        """Two concurrent calls with max_concurrent=2 both succeed."""
        inner = _make_inner(delay=0.05)
        module = QueueModule({"max_concurrent": 2, "call_timeout": 5.0}, inner)

        results = await asyncio.gather(
            module.invoke(messages),
            module.invoke(messages),
        )
        assert len(results) == 2
        assert all(r.content == "ok" for r in results)
        assert inner.invoke.await_count == 2

    async def test_provider_timeout_is_not_queue_timeout(self, messages):
        inner = _make_inner(side_effect=[TimeoutError("provider timed out")])
        module = QueueModule({"max_concurrent": 1}, inner)
        with pytest.raises(TimeoutError, match="provider timed out"):
            await module.invoke(messages)
        assert module.queue_stats()["total_timeouts"] == 0

    async def test_concurrency_limits_enforced(self, messages):
        """3rd call waits when max_concurrent=2, proceeds after first completes."""
        call_order: list[int] = []
        call_count = 0

        async def _tracked_invoke(*_args, **_kwargs):
            nonlocal call_count
            idx = call_count
            call_count += 1
            call_order.append(idx)
            # First two calls take 0.1s, third call is instant
            if idx < 2:
                await asyncio.sleep(0.1)
            return _OK_RESPONSE

        inner = MagicMock(spec=LLMProvider)
        inner.name = "test-provider"
        inner.model_name = "test-model"
        inner.validate_config.return_value = True
        inner.invoke = AsyncMock(side_effect=_tracked_invoke)

        module = QueueModule(
            {"max_concurrent": 2, "call_timeout": 5.0, "max_queued": 5},
            inner,
        )

        results = await asyncio.gather(
            module.invoke(messages),
            module.invoke(messages),
            module.invoke(messages),
        )
        assert len(results) == 3
        assert inner.invoke.await_count == 3


class TestStreamingQueue:
    async def test_stream_and_invoke_share_capacity(self, messages):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def stream(*_args, **_kwargs):
            entered.set()
            await release.wait()
            yield Delta(text="ok")

        inner = _make_inner()
        inner.invoke_stream = stream
        module = QueueModule({"max_concurrent": 1, "max_queued": 1}, inner)
        stream_task = asyncio.create_task(_collect(module.invoke_stream(messages)))
        await entered.wait()
        invoke_task = asyncio.create_task(module.invoke(messages))
        await asyncio.sleep(0)
        assert module.queue_stats()["active"] == 1
        assert module.queue_stats()["waiting"] == 1
        assert inner.invoke.await_count == 0
        release.set()
        assert [delta.text for delta in await stream_task] == ["ok"]
        await invoke_task
        assert module.queue_stats()["active"] == 0
        assert module.queue_stats()["waiting"] == 0

    async def test_stream_send_timeout_and_cleanup(self, messages):
        async def stream(*_args, **_kwargs):
            await asyncio.sleep(1)
            yield Delta(text="late")

        inner = _make_inner()
        inner.invoke_stream = stream
        module = QueueModule({"max_concurrent": 1, "call_timeout": 0.02}, inner)
        with pytest.raises(QueueTimeoutError):
            await _collect(module.invoke_stream(messages))
        assert module.queue_stats()["active"] == 0
        assert module.queue_stats()["total_timeouts"] == 1

    async def test_stream_close_releases_slot(self, messages):
        closed = asyncio.Event()

        async def stream(*_args, **_kwargs):
            try:
                yield Delta(text="first")
                await asyncio.sleep(1)
            finally:
                closed.set()

        inner = _make_inner()
        inner.invoke_stream = stream
        module = QueueModule({"max_concurrent": 1}, inner)
        iterator = module.invoke_stream(messages)
        assert (await anext(iterator)).text == "first"
        await iterator.aclose()
        assert closed.is_set()
        assert module.queue_stats()["active"] == 0
        await module.invoke(messages)

    async def test_stream_backpressure_counts_invoke_waiter(self, messages):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def stream(*_args, **_kwargs):
            entered.set()
            await release.wait()
            yield Delta(text="ok")

        inner = _make_inner()
        inner.invoke_stream = stream
        module = QueueModule({"max_concurrent": 1, "max_queued": 1}, inner)
        active = asyncio.create_task(_collect(module.invoke_stream(messages)))
        await entered.wait()
        waiting = asyncio.create_task(module.invoke(messages))
        await asyncio.sleep(0)
        with pytest.raises(QueueFullError):
            await _collect(module.invoke_stream(messages))
        release.set()
        await active
        await waiting
        assert module.queue_stats()["total_rejected"] == 1

    async def test_cancel_waiting_stream_does_not_leak_waiter(self, messages):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def stream(*_args, **_kwargs):
            entered.set()
            await release.wait()
            yield Delta(text="ok")

        inner = _make_inner()
        inner.invoke_stream = stream
        module = QueueModule({"max_concurrent": 1, "max_queued": 1}, inner)
        active = asyncio.create_task(_collect(module.invoke_stream(messages)))
        await entered.wait()
        waiting = asyncio.create_task(_collect(module.invoke_stream(messages)))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert module.queue_stats()["waiting"] == 0
        release.set()
        await active
        assert module.queue_stats()["active"] == 0

    async def test_stream_provider_error_releases_slot(self, messages):
        async def stream(*_args, **_kwargs):
            yield Delta(text="partial")
            raise RuntimeError("provider disconnected")

        inner = _make_inner()
        inner.invoke_stream = stream
        module = QueueModule({"max_concurrent": 1}, inner)
        with pytest.raises(RuntimeError, match="provider disconnected"):
            await _collect(module.invoke_stream(messages))
        assert module.queue_stats()["active"] == 0
        await module.invoke(messages)

    async def test_provider_timeout_is_not_counted_as_queue_timeout(self, messages):
        async def stream(*_args, **_kwargs):
            raise TimeoutError("provider timed out")
            yield Delta(text="never")

        inner = _make_inner()
        inner.invoke_stream = stream
        module = QueueModule({"max_concurrent": 1}, inner)
        with pytest.raises(TimeoutError, match="provider timed out"):
            await _collect(module.invoke_stream(messages))
        assert module.queue_stats()["total_timeouts"] == 0
        assert module.queue_stats()["active"] == 0

    async def test_cleanup_error_does_not_hide_provider_failure(self, messages):
        class FailingStream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise RuntimeError("provider failed")

            async def aclose(self):
                raise RuntimeError("cleanup failed")

        inner = _make_inner()
        inner.invoke_stream = lambda *_args, **_kwargs: FailingStream()
        module = QueueModule({"max_concurrent": 1}, inner)
        with pytest.raises(RuntimeError, match="provider failed"):
            await _collect(module.invoke_stream(messages))
        assert module.queue_stats()["active"] == 0

    async def test_cancelling_active_stream_releases_slot(self, messages):
        entered = asyncio.Event()
        closed = asyncio.Event()

        async def stream(*_args, **_kwargs):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield Delta(text="never")
            finally:
                closed.set()

        inner = _make_inner()
        inner.invoke_stream = stream
        module = QueueModule({"max_concurrent": 1}, inner)
        task = asyncio.create_task(_collect(module.invoke_stream(messages)))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
        assert module.queue_stats()["active"] == 0

    async def test_stream_timeout_after_first_delta(self, messages):
        async def stream(*_args, **_kwargs):
            yield Delta(text="partial")
            await asyncio.sleep(1)
            yield Delta(text="late")

        inner = _make_inner()
        inner.invoke_stream = stream
        module = QueueModule({"max_concurrent": 1, "call_timeout": 0.02}, inner)
        iterator = module.invoke_stream(messages)
        assert (await anext(iterator)).text == "partial"
        with pytest.raises(QueueTimeoutError):
            await anext(iterator)
        assert module.queue_stats()["active"] == 0

    async def test_slow_consumer_is_not_cancelled_between_deltas(self, messages):
        async def stream(*_args, **_kwargs):
            yield Delta(text="first")
            yield Delta(text="second")

        inner = _make_inner()
        inner.invoke_stream = stream
        module = QueueModule({"max_concurrent": 1, "call_timeout": 0.02}, inner)
        iterator = module.invoke_stream(messages)
        assert (await anext(iterator)).text == "first"
        await asyncio.sleep(0.04)
        with pytest.raises(QueueTimeoutError):
            await anext(iterator)
        assert module.queue_stats()["active"] == 0

    async def test_stream_can_be_consumed_across_tasks(self, messages):
        async def stream(*_args, **_kwargs):
            yield Delta(text="first")
            yield Delta(text="second")

        inner = _make_inner()
        inner.invoke_stream = stream
        module = QueueModule({"max_concurrent": 1}, inner)
        iterator = module.invoke_stream(messages)
        assert (await asyncio.create_task(anext(iterator))).text == "first"
        assert (await asyncio.create_task(anext(iterator))).text == "second"
        await asyncio.create_task(iterator.aclose())
        assert module.queue_stats()["active"] == 0

    async def test_zero_wait_slots_admit_immediate_calls_only(self, messages):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def stream(*_args, **_kwargs):
            entered.set()
            await release.wait()
            yield Delta(text="ok")

        inner = _make_inner()
        inner.invoke_stream = stream
        module = QueueModule({"max_concurrent": 1, "max_queued": 0}, inner)
        active = asyncio.create_task(_collect(module.invoke_stream(messages)))
        await entered.wait()
        with pytest.raises(QueueFullError):
            await module.invoke(messages)
        release.set()
        await active
        await module.invoke(messages)

    async def test_default_load_model_queue_gates_stream(self, messages, monkeypatch):
        from arcllm.adapters.anthropic import AnthropicAdapter
        from arcllm.registry import load_model

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
        closed = asyncio.Event()

        async def stream(*_args, **_kwargs):
            try:
                yield Delta(text="first")
                yield Delta(text="second")
            finally:
                closed.set()

        with patch.object(AnthropicAdapter, "invoke_stream", stream):
            model = load_model("anthropic")
            assert isinstance(model, QueueModule)
            deltas = await _collect(model.invoke_stream(messages))
            assert "".join(delta.text for delta in deltas) == "firstsecond"
            assert closed.is_set()
            assert model.queue_stats()["total_completed"] == 1
            assert model.queue_stats()["active"] == 0
            await model.close()

    @pytest.mark.parametrize("with_inner_modules", [False, True])
    async def test_loaded_wrapper_chain_closes_stream_and_releases_slot(
        self, messages, monkeypatch, with_inner_modules
    ):
        from arcllm.adapters.anthropic import AnthropicAdapter
        from arcllm.modules.otel import OtelModule
        from arcllm.modules.telemetry import TelemetryModule
        from arcllm.registry import load_model

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
        closed = asyncio.Event()

        async def stream(*_args, **_kwargs):
            try:
                yield Delta(text="first")
                yield Delta(text="second")
            finally:
                closed.set()

        with patch.object(AnthropicAdapter, "invoke_stream", stream):
            model = load_model(
                "anthropic",
                queue={"max_concurrent": 1, "max_queued": 0},
                otel={"exporter": "none"},
                telemetry=with_inner_modules,
                audit=with_inner_modules,
                security=False,
                retry=False,
                fallback=with_inner_modules,
                rate_limit=with_inner_modules,
                circuit_breaker=False,
                injection=False,
                guardrails=False,
            )
            assert isinstance(model, OtelModule)
            queue = model._inner
            assert isinstance(queue, QueueModule)
            if with_inner_modules:
                telemetry = queue._inner
                assert isinstance(telemetry, TelemetryModule)
                spool = MagicMock()
                telemetry._record_spool = spool
            iterator = model.invoke_stream(messages)
            assert (await anext(iterator)).text == "first"
            assert queue.queue_stats()["active"] == 1
            with pytest.raises(QueueFullError):
                await _collect(model.invoke_stream(messages))
            await asyncio.create_task(iterator.aclose())
            assert closed.is_set()
            assert queue.queue_stats()["active"] == 0
            if with_inner_modules:
                assert not spool.called
            await model.close()


async def _collect(iterator):
    return [delta async for delta in iterator]


# ---------------------------------------------------------------------------
# TestQueueBackpressure
# ---------------------------------------------------------------------------


class TestQueueBackpressure:
    """R-003: Reject when max_queued exceeded."""

    async def test_backpressure_rejects_when_full(self, messages):
        """Raises QueueFullError when max_queued waiters exceeded."""
        # Slow inner so calls pile up in the queue
        inner = _make_inner(delay=1.0)
        module = QueueModule(
            {"max_concurrent": 1, "call_timeout": 5.0, "max_queued": 1},
            inner,
        )

        async def _fire_and_catch():
            """Fire a call and return any QueueFullError, or None on success."""
            try:
                await module.invoke(messages)
                return None
            except QueueFullError as e:
                return e

        # First call: acquires semaphore (in-flight)
        # Second call: waits (1 waiter — at max_queued)
        # Third call: should be rejected (exceeds max_queued)
        tasks = [
            asyncio.create_task(_fire_and_catch()),
            asyncio.create_task(_fire_and_catch()),
        ]
        # Small delay to let first two start
        await asyncio.sleep(0.05)

        # This third call should hit backpressure
        third_result = await _fire_and_catch()

        # Cancel the long-running tasks to clean up
        for t in tasks:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

        assert isinstance(third_result, QueueFullError)
        assert third_result.max_queued == 1


# ---------------------------------------------------------------------------
# TestQueueTimeout
# ---------------------------------------------------------------------------


class TestQueueTimeout:
    """R-002: Send-time timeout (starts after semaphore acquired)."""

    async def test_send_time_timeout_raises_error(self, messages):
        """Raises QueueTimeoutError when inner.invoke() exceeds call_timeout."""
        # Inner takes 1s but timeout is 0.05s
        inner = _make_inner(delay=1.0)
        module = QueueModule(
            {"max_concurrent": 2, "call_timeout": 0.05},
            inner,
        )

        with pytest.raises(QueueTimeoutError) as exc_info:
            await module.invoke(messages)
        assert exc_info.value.timeout == 0.05

    async def test_queue_wait_excluded_from_timeout(self, messages):
        """Long queue wait + fast inner call succeeds (timeout only covers inner)."""
        call_count = 0

        async def _invoke_with_delay(*_args, **_kwargs):
            nonlocal call_count
            idx = call_count
            call_count += 1
            if idx == 0:
                # First call: slow (blocks the semaphore for 0.2s)
                await asyncio.sleep(0.2)
            # Subsequent calls: fast
            return _OK_RESPONSE

        inner = MagicMock(spec=LLMProvider)
        inner.name = "test-provider"
        inner.model_name = "test-model"
        inner.validate_config.return_value = True
        inner.invoke = AsyncMock(side_effect=_invoke_with_delay)

        # call_timeout=0.5 is longer than inner call but shorter than
        # queue_wait + inner call if timeout started at enqueue time.
        module = QueueModule(
            {"max_concurrent": 1, "call_timeout": 0.5, "max_queued": 5},
            inner,
        )

        # Both calls succeed — second one waits ~0.2s in queue but
        # its 0.5s timeout starts only after acquiring the semaphore.
        results = await asyncio.gather(
            module.invoke(messages),
            module.invoke(messages),
        )
        assert len(results) == 2
        assert all(r.content == "ok" for r in results)


# ---------------------------------------------------------------------------
# TestQueueOtel
# ---------------------------------------------------------------------------


class TestQueueOtel:
    """R-004: Otel span attributes set on active span."""

    async def test_otel_span_attributes_set(self, messages):
        """Verifies arc.queue.* span attributes are set."""
        inner = _make_inner()
        module = QueueModule(
            {"max_concurrent": 2, "call_timeout": 60.0},
            inner,
        )

        mock_span = MagicMock()
        mock_span.is_recording.return_value = True

        with patch("arcllm.modules.queue.trace.get_current_span", return_value=mock_span):
            await module.invoke(messages)

        # Verify span attributes were set
        set_calls = {call.args[0]: call.args[1] for call in mock_span.set_attribute.call_args_list}
        assert "arc.queue.wait_ms" in set_calls
        assert "arc.queue.depth" in set_calls
        assert "arc.queue.call_timeout_ms" in set_calls
        assert set_calls["arc.queue.call_timeout_ms"] == 60000

    async def test_otel_rejected_attribute_set_on_backpressure(self, messages):
        """On rejection, arc.queue.rejected=True is set before raising."""
        inner = _make_inner(delay=1.0)
        module = QueueModule(
            {"max_concurrent": 1, "call_timeout": 5.0, "max_queued": 1},
            inner,
        )

        mock_span = MagicMock()
        mock_span.is_recording.return_value = True

        # First call acquires semaphore (in-flight)
        task1 = asyncio.create_task(module.invoke(messages))
        await asyncio.sleep(0.05)
        # Second call waits (1 waiter — fills max_queued)
        task2 = asyncio.create_task(module.invoke(messages))
        await asyncio.sleep(0.05)

        # Third call should be rejected (waiters >= max_queued)
        with patch("arcllm.modules.queue.trace.get_current_span", return_value=mock_span):
            with pytest.raises(QueueFullError):
                await module.invoke(messages)

        set_calls = {call.args[0]: call.args[1] for call in mock_span.set_attribute.call_args_list}
        assert set_calls.get("arc.queue.rejected") is True

        for t in (task1, task2):
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# TestQueueConfigValidation
# ---------------------------------------------------------------------------


class TestQueueConfigValidation:
    """Config key and value validation."""

    def test_invalid_config_key_rejected(self):
        inner = _make_inner()
        with pytest.raises(ArcLLMConfigError, match="Unknown queue"):
            QueueModule({"bad_key": 1}, inner)

    def test_max_concurrent_zero_rejected(self):
        inner = _make_inner()
        with pytest.raises(ArcLLMConfigError, match="max_concurrent must be >= 1"):
            QueueModule({"max_concurrent": 0}, inner)

    def test_max_concurrent_negative_rejected(self):
        inner = _make_inner()
        with pytest.raises(ArcLLMConfigError, match="max_concurrent must be >= 1"):
            QueueModule({"max_concurrent": -1}, inner)

    def test_call_timeout_zero_rejected(self):
        inner = _make_inner()
        with pytest.raises(ArcLLMConfigError, match="call_timeout must be > 0"):
            QueueModule({"call_timeout": 0}, inner)

    def test_max_queued_negative_rejected(self):
        inner = _make_inner()
        with pytest.raises(ArcLLMConfigError, match="max_queued must be >= 0"):
            QueueModule({"max_queued": -1}, inner)
