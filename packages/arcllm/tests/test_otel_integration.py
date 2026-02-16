"""Tests for span creation in existing modules — retry, fallback, rate_limit, telemetry, audit."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opentelemetry.trace import StatusCode

from arcllm.exceptions import ArcLLMAPIError
from arcllm.modules.rate_limit import clear_buckets
from arcllm.types import LLMResponse, Message, Usage


def _make_response(**overrides) -> LLMResponse:
    """Create a mock LLMResponse."""
    defaults = dict(
        content="hello",
        tool_calls=[],
        usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
        model="test-model",
        stop_reason="end_turn",
    )
    defaults.update(overrides)
    return LLMResponse(**defaults)


def _make_inner(response=None, side_effect=None) -> MagicMock:
    """Create a mock inner LLMProvider."""
    inner = MagicMock()
    inner.name = "test_provider"
    inner.model_name = "test-model"
    if side_effect:
        inner.invoke = AsyncMock(side_effect=side_effect)
    else:
        inner.invoke = AsyncMock(return_value=response or _make_response())
    return inner


def _make_mock_tracer():
    """Create a mock tracer and span for assertion."""
    mock_tracer = MagicMock()
    spans = []

    def make_span_context(name, attributes=None):
        span = MagicMock()
        span._name = name
        spans.append(span)
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=span)
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    mock_tracer.start_as_current_span = make_span_context
    return mock_tracer, spans


@pytest.fixture(autouse=True)
def _clear_rate_limit_state():
    """Clear rate limit buckets between tests."""
    clear_buckets()
    yield
    clear_buckets()


class TestRetrySpans:
    """RetryModule span tests."""

    @pytest.mark.asyncio
    async def test_retry_creates_retry_span(self):
        """arcllm.retry span exists."""
        from arcllm.modules.retry import RetryModule

        inner = _make_inner()
        module = RetryModule({"max_retries": 1}, inner)
        mock_tracer, spans = _make_mock_tracer()
        with patch("arcllm.modules.base.trace.get_tracer", return_value=mock_tracer):
            messages = [Message(role="user", content="hi")]
            await module.invoke(messages)
        span_names = [s._name for s in spans]
        assert "arcllm.retry" in span_names

    @pytest.mark.asyncio
    async def test_retry_creates_attempt_spans(self):
        """arcllm.retry.attempt span created per attempt."""
        from arcllm.modules.retry import RetryModule

        error = ArcLLMAPIError(429, "rate limited", "test")
        inner = _make_inner(
            side_effect=[error, _make_response()]
        )
        module = RetryModule(
            {"max_retries": 2, "backoff_base_seconds": 0.001}, inner
        )
        mock_tracer, spans = _make_mock_tracer()
        with patch("arcllm.modules.base.trace.get_tracer", return_value=mock_tracer):
            messages = [Message(role="user", content="hi")]
            await module.invoke(messages)
        span_names = [s._name for s in spans]
        assert span_names.count("arcllm.retry.attempt") == 2

    @pytest.mark.asyncio
    async def test_retry_records_exception_on_failed_attempt(self):
        """Exception event recorded on failed attempt span."""
        from arcllm.modules.retry import RetryModule

        error = ArcLLMAPIError(429, "rate limited", "test")
        inner = _make_inner(
            side_effect=[error, _make_response()]
        )
        module = RetryModule(
            {"max_retries": 2, "backoff_base_seconds": 0.001}, inner
        )
        mock_tracer, spans = _make_mock_tracer()
        with patch("arcllm.modules.base.trace.get_tracer", return_value=mock_tracer):
            messages = [Message(role="user", content="hi")]
            await module.invoke(messages)
        # First attempt span should have exception recorded
        attempt_spans = [s for s in spans if s._name == "arcllm.retry.attempt"]
        attempt_spans[0].record_exception.assert_called_once()

    @pytest.mark.asyncio
    async def test_retry_attempt_ok_when_handled(self):
        """StatusCode.OK on retried attempt (error was handled)."""
        from arcllm.modules.retry import RetryModule

        error = ArcLLMAPIError(429, "rate limited", "test")
        inner = _make_inner(
            side_effect=[error, _make_response()]
        )
        module = RetryModule(
            {"max_retries": 2, "backoff_base_seconds": 0.001}, inner
        )
        mock_tracer, spans = _make_mock_tracer()
        with patch("arcllm.modules.base.trace.get_tracer", return_value=mock_tracer):
            messages = [Message(role="user", content="hi")]
            await module.invoke(messages)
        attempt_spans = [s for s in spans if s._name == "arcllm.retry.attempt"]
        attempt_spans[0].set_status.assert_called_with(StatusCode.OK)

    @pytest.mark.asyncio
    async def test_retry_error_on_exhaustion(self):
        """StatusCode.ERROR when all retries fail."""
        from arcllm.modules.retry import RetryModule

        error = ArcLLMAPIError(429, "rate limited", "test")
        inner = _make_inner(side_effect=[error, error])
        module = RetryModule(
            {"max_retries": 1, "backoff_base_seconds": 0.001}, inner
        )
        mock_tracer, spans = _make_mock_tracer()
        with patch("arcllm.modules.base.trace.get_tracer", return_value=mock_tracer):
            messages = [Message(role="user", content="hi")]
            with pytest.raises(ArcLLMAPIError):
                await module.invoke(messages)
        retry_span = next(s for s in spans if s._name == "arcllm.retry")
        # set_status now includes error description from _span()
        call_args = retry_span.set_status.call_args
        assert call_args[0][0] == StatusCode.ERROR


class TestFallbackSpans:
    """FallbackModule span tests."""

    @pytest.mark.asyncio
    async def test_fallback_creates_fallback_span(self):
        """arcllm.fallback span exists."""
        from arcllm.modules.fallback import FallbackModule

        inner = _make_inner()
        module = FallbackModule({"chain": []}, inner)
        mock_tracer, spans = _make_mock_tracer()
        with patch("arcllm.modules.base.trace.get_tracer", return_value=mock_tracer):
            messages = [Message(role="user", content="hi")]
            await module.invoke(messages)
        span_names = [s._name for s in spans]
        assert "arcllm.fallback" in span_names

    @pytest.mark.asyncio
    async def test_fallback_creates_provider_spans(self):
        """Per-provider child spans created during fallback."""
        from arcllm.modules.fallback import FallbackModule

        inner = _make_inner(side_effect=RuntimeError("primary fail"))
        fallback_provider = _make_inner()
        fallback_provider.close = AsyncMock()
        module = FallbackModule({"chain": ["backup"]}, inner)
        mock_tracer, spans = _make_mock_tracer()
        with (
            patch("arcllm.modules.base.trace.get_tracer", return_value=mock_tracer),
            patch(
                "arcllm.modules.fallback.load_model", return_value=fallback_provider
            ),
        ):
            messages = [Message(role="user", content="hi")]
            await module.invoke(messages)
        span_names = [s._name for s in spans]
        assert "arcllm.fallback.attempt" in span_names

    @pytest.mark.asyncio
    async def test_fallback_primary_failed_event(self):
        """Event recorded when primary fails."""
        from arcllm.modules.fallback import FallbackModule

        inner = _make_inner(side_effect=RuntimeError("primary fail"))
        fallback_provider = _make_inner()
        fallback_provider.close = AsyncMock()
        module = FallbackModule({"chain": ["backup"]}, inner)
        mock_tracer, spans = _make_mock_tracer()
        with (
            patch("arcllm.modules.base.trace.get_tracer", return_value=mock_tracer),
            patch(
                "arcllm.modules.fallback.load_model", return_value=fallback_provider
            ),
        ):
            messages = [Message(role="user", content="hi")]
            await module.invoke(messages)
        fallback_span = next(s for s in spans if s._name == "arcllm.fallback")
        fallback_span.add_event.assert_called()


class TestRateLimitSpans:
    """RateLimitModule span tests."""

    @pytest.mark.asyncio
    async def test_rate_limit_creates_span(self):
        """arcllm.rate_limit span exists."""
        from arcllm.modules.rate_limit import RateLimitModule

        inner = _make_inner()
        module = RateLimitModule({"requests_per_minute": 60}, inner)
        mock_tracer, spans = _make_mock_tracer()
        with patch("arcllm.modules.base.trace.get_tracer", return_value=mock_tracer):
            messages = [Message(role="user", content="hi")]
            await module.invoke(messages)
        span_names = [s._name for s in spans]
        assert "arcllm.rate_limit" in span_names

    @pytest.mark.asyncio
    async def test_rate_limit_records_wait_ms(self):
        """arcllm.rate_limit.wait_ms attribute set."""
        from arcllm.modules.rate_limit import RateLimitModule

        inner = _make_inner()
        module = RateLimitModule({"requests_per_minute": 60}, inner)
        mock_tracer, spans = _make_mock_tracer()
        with patch("arcllm.modules.base.trace.get_tracer", return_value=mock_tracer):
            messages = [Message(role="user", content="hi")]
            await module.invoke(messages)
        rl_span = next(s for s in spans if s._name == "arcllm.rate_limit")
        rl_span.set_attribute.assert_any_call("arcllm.rate_limit.wait_ms", pytest.approx(0.0, abs=100))

    @pytest.mark.asyncio
    async def test_rate_limit_throttled_event(self):
        """Event recorded when throttled (wait > 0)."""
        from arcllm.modules.rate_limit import RateLimitModule, TokenBucket

        inner = _make_inner()
        module = RateLimitModule({"requests_per_minute": 60}, inner)
        # Force the bucket to return non-zero wait
        with patch.object(module._bucket, "acquire", return_value=0.5):
            mock_tracer, spans = _make_mock_tracer()
            with patch(
                "arcllm.modules.base.trace.get_tracer", return_value=mock_tracer
            ):
                messages = [Message(role="user", content="hi")]
                await module.invoke(messages)
        rl_span = next(s for s in spans if s._name == "arcllm.rate_limit")
        rl_span.add_event.assert_called()


class TestTelemetrySpans:
    """TelemetryModule span tests."""

    @pytest.mark.asyncio
    async def test_telemetry_creates_span(self):
        """arcllm.telemetry span exists."""
        from arcllm.modules.telemetry import TelemetryModule

        inner = _make_inner()
        module = TelemetryModule({}, inner)
        mock_tracer, spans = _make_mock_tracer()
        with patch("arcllm.modules.base.trace.get_tracer", return_value=mock_tracer):
            messages = [Message(role="user", content="hi")]
            await module.invoke(messages)
        span_names = [s._name for s in spans]
        assert "arcllm.telemetry" in span_names

    @pytest.mark.asyncio
    async def test_telemetry_records_duration_and_cost(self):
        """duration_ms and cost_usd attributes set on span."""
        from arcllm.modules.telemetry import TelemetryModule

        inner = _make_inner()
        module = TelemetryModule(
            {"cost_input_per_1m": 3.0, "cost_output_per_1m": 15.0}, inner
        )
        mock_tracer, spans = _make_mock_tracer()
        with patch("arcllm.modules.base.trace.get_tracer", return_value=mock_tracer):
            messages = [Message(role="user", content="hi")]
            await module.invoke(messages)
        tel_span = next(s for s in spans if s._name == "arcllm.telemetry")
        attr_calls = {c[0][0]: c[0][1] for c in tel_span.set_attribute.call_args_list}
        assert "arcllm.telemetry.duration_ms" in attr_calls
        assert "arcllm.telemetry.cost_usd" in attr_calls


class TestAuditSpans:
    """AuditModule span tests."""

    @pytest.mark.asyncio
    async def test_audit_creates_span(self):
        """arcllm.audit span exists."""
        from arcllm.modules.audit import AuditModule

        inner = _make_inner()
        module = AuditModule({}, inner)
        mock_tracer, spans = _make_mock_tracer()
        with patch("arcllm.modules.base.trace.get_tracer", return_value=mock_tracer):
            messages = [Message(role="user", content="hi")]
            await module.invoke(messages)
        span_names = [s._name for s in spans]
        assert "arcllm.audit" in span_names

    @pytest.mark.asyncio
    async def test_audit_records_metadata(self):
        """message_count and content_length attributes set on span."""
        from arcllm.modules.audit import AuditModule

        inner = _make_inner()
        module = AuditModule({}, inner)
        mock_tracer, spans = _make_mock_tracer()
        with patch("arcllm.modules.base.trace.get_tracer", return_value=mock_tracer):
            messages = [Message(role="user", content="hi")]
            await module.invoke(messages)
        audit_span = next(s for s in spans if s._name == "arcllm.audit")
        attr_calls = {
            c[0][0]: c[0][1] for c in audit_span.set_attribute.call_args_list
        }
        assert attr_calls.get("arcllm.audit.message_count") == 1
        assert "arcllm.audit.content_length" in attr_calls
