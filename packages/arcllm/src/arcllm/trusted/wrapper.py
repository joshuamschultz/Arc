"""Base wrapper class for Arc-owned middleware.

This module provides the foundation for middleware that applies Arc duties
(telemetry, audit, security, etc.) uniformly to any backend. The wrapper
depends only on arcllm.contract types plus arcstore and arctrust — it never
imports backends or adapters.

If the trusted wrapper moves (to arcrun, or a new package):
- Move this entire arcllm/trusted/ directory
- Update the factory import in arcllm/__init__.py or arcllm/loader.py
- Backends and contract stay in arcllm
"""

from abc import abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from arcllm.contract.types import Delta, LLMResponse, Message, ModelProvider, Tool


class TrustedWrapperBase:
    """Base class for Arc-owned middleware that wraps any backend.

    Each concrete middleware (TelemetryMiddleware, SecurityMiddleware, etc.)
    extends this and implements invoke/invoke_stream to apply one Arc duty
    before/after delegating to the wrapped provider.

    The wrapper forms a chain via the .inner property. Consumers walk the
    chain without isinstance checks.
    """

    def __init__(self, inner: ModelProvider) -> None:
        self._inner = inner

    @property
    def inner(self) -> ModelProvider:
        """The wrapped provider."""
        return self._inner

    @property
    def name(self) -> str:
        """Delegate to wrapped provider."""
        return self._inner.name

    @property
    def model_name(self) -> str:
        """Delegate to wrapped provider."""
        return self._inner.model_name

    @property
    def max_output_tokens(self) -> int | None:
        """Delegate to wrapped provider."""
        return self._inner.max_output_tokens

    @abstractmethod
    async def invoke(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Wrap the call with middleware behavior."""

    @abstractmethod
    async def invoke_stream(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[Delta]:
        """Wrap the stream with middleware behavior."""

    def validate_config(self) -> bool:
        """Delegate to wrapped provider."""
        return self._inner.validate_config()

    async def close(self) -> None:
        """Delegate to wrapped provider."""
        await self._inner.close()


__all__ = ["TrustedWrapperBase"]
