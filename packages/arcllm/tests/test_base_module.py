"""Tests for BaseModule span support — _tracer property and _span() context manager."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from arcllm.modules.base import BaseModule
from arcllm.types import LLMResponse, Message, Usage


def _make_inner() -> MagicMock:
    """Create a mock inner LLMProvider."""
    inner = MagicMock()
    inner.name = "test_provider"
    inner.model_name = "test-model"
    inner.invoke = AsyncMock(
        return_value=LLMResponse(
            content="hello",
            tool_calls=[],
            usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
            model="test-model",
            stop_reason="end_turn",
        )
    )
    return inner


class TestBaseModuleTracer:
    """Tests for _tracer property."""

    def test_tracer_returns_tracer(self):
        """_tracer returns an OTel Tracer instance."""
        module = BaseModule({}, _make_inner())
        tracer = module._tracer
        assert isinstance(tracer, trace.ProxyTracer)

    def test_tracer_uses_arcllm_name(self):
        """_tracer calls get_tracer with 'arcllm'."""
        module = BaseModule({}, _make_inner())
        with patch("arcllm.modules.base.trace.get_tracer") as mock_get:
            mock_get.return_value = MagicMock()
            _ = module._tracer
            mock_get.assert_called_once_with("arcllm")


class TestBaseModuleSpan:
    """Tests for _span() context manager."""

    def test_span_creates_named_span(self):
        """_span('name') creates a span with that name."""
        module = BaseModule({}, _make_inner())
        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(
            return_value=mock_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(
            return_value=False
        )
        with patch("arcllm.modules.base.trace.get_tracer", return_value=mock_tracer):
            with module._span("test.span"):
                pass
        mock_tracer.start_as_current_span.assert_called_once_with(
            "test.span", attributes=None
        )

    def test_span_yields_span_object(self):
        """_span() context manager yields the span."""
        module = BaseModule({}, _make_inner())
        with module._span("test.span") as span:
            # No-op tracer returns a NonRecordingSpan
            assert span is not None

    def test_span_records_exception_on_error(self):
        """Unhandled exception is recorded on the span."""
        module = BaseModule({}, _make_inner())
        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(
            return_value=mock_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(
            return_value=False
        )
        with patch("arcllm.modules.base.trace.get_tracer", return_value=mock_tracer):
            with pytest.raises(ValueError, match="boom"):
                with module._span("test.span"):
                    raise ValueError("boom")
        mock_span.record_exception.assert_called_once()

    def test_span_sets_error_status_on_error(self):
        """StatusCode.ERROR is set on the span when an error occurs."""
        module = BaseModule({}, _make_inner())
        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(
            return_value=mock_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(
            return_value=False
        )
        with patch("arcllm.modules.base.trace.get_tracer", return_value=mock_tracer):
            with pytest.raises(ValueError):
                with module._span("test.span"):
                    raise ValueError("boom")
        mock_span.set_status.assert_called_once_with(StatusCode.ERROR, "boom")

    def test_span_reraises_exception(self):
        """Exception propagates to caller — _span() is transparent."""
        module = BaseModule({}, _make_inner())
        with pytest.raises(ValueError, match="boom"):
            with module._span("test.span"):
                raise ValueError("boom")

    def test_span_accepts_attributes(self):
        """Attributes dict is passed to the span."""
        module = BaseModule({}, _make_inner())
        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(
            return_value=mock_span
        )
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(
            return_value=False
        )
        attrs = {"key": "value", "count": 42}
        with patch("arcllm.modules.base.trace.get_tracer", return_value=mock_tracer):
            with module._span("test.span", attributes=attrs):
                pass
        mock_tracer.start_as_current_span.assert_called_once_with(
            "test.span", attributes=attrs
        )

    def test_span_noop_without_sdk(self):
        """No crash when tracer is no-op (SDK not configured)."""
        module = BaseModule({}, _make_inner())
        # Without SDK, tracer is no-op — should not crash
        with module._span("test.span") as span:
            assert span is not None

    def test_nested_spans_parent_child(self):
        """Inner _span() auto-parents under outer span."""
        module = BaseModule({}, _make_inner())
        # Just verify no crash with nested spans (no-op tracer)
        with module._span("outer") as outer_span:
            with module._span("inner") as inner_span:
                assert outer_span is not None
                assert inner_span is not None


class TestBaseModuleInvokeUnchanged:
    """Verify existing BaseModule.invoke() behavior is unaffected."""

    @pytest.mark.asyncio
    async def test_invoke_unchanged(self):
        """invoke() delegates to inner and returns response unchanged."""
        inner = _make_inner()
        module = BaseModule({}, inner)
        messages = [Message(role="user", content="hi")]
        response = await module.invoke(messages)
        inner.invoke.assert_awaited_once_with(messages, None)
        assert response.content == "hello"
