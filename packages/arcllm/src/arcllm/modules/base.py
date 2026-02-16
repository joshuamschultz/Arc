"""BaseModule — transparent wrapper foundation for all modules."""

import contextlib
from collections.abc import Generator
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import StatusCode

from arcllm.types import LLMProvider, LLMResponse, Message, Tool


class BaseModule(LLMProvider):
    """Base class for ArcLLM modules.

    Wraps an inner LLMProvider and delegates all calls by default.
    Subclasses override invoke() to add behavior (retry, fallback, etc.).

    Provides ``_tracer`` and ``_span()`` for OpenTelemetry span creation.
    """

    def __init__(self, config: dict[str, Any], inner: LLMProvider) -> None:
        self._config = config
        self._inner = inner

    @property
    def _tracer(self) -> trace.Tracer:
        """Return an OTel Tracer scoped to 'arcllm'."""
        return trace.get_tracer("arcllm")

    @contextlib.contextmanager
    def _span(
        self, name: str, attributes: dict[str, Any] | None = None
    ) -> Generator[trace.Span, None, None]:
        """Create a named OTel span as a context manager.

        Records exceptions and sets ERROR status on unhandled errors,
        then re-raises. No-op when no SDK is configured (tracer returns
        NonRecordingSpan).
        """
        with self._tracer.start_as_current_span(name, attributes=attributes) as span:
            try:
                yield span
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(StatusCode.ERROR, str(exc))
                raise

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def model_name(self) -> str:
        return self._inner.model_name

    async def invoke(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        return await self._inner.invoke(messages, tools, **kwargs)

    def validate_config(self) -> bool:
        return self._inner.validate_config()

    async def close(self) -> None:
        """Close resources held by the inner provider."""
        if hasattr(self._inner, "close"):
            await self._inner.close()
