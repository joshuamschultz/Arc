"""Middleware protocol and Arc-private kwargs filter.

NO imports of other Arc packages. The middleware protocol allows Arc's trusted
layer to wrap any backend uniformly.
"""

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

# Forward references - will be resolved when imported alongside types
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arcllm.contract.types import Delta, LLMResponse, Message, ModelProvider, Tool


@runtime_checkable
class Middleware(Protocol):
    """Protocol for middleware that wraps a ModelProvider.

    Middleware forms a chain: each layer wraps an inner provider and may
    observe, transform, or block calls. The .inner property allows walking
    the chain without isinstance checks.
    """

    @property
    def inner(self) -> "ModelProvider":
        """The wrapped provider. Allows walking the middleware chain."""

    @property
    def name(self) -> str:
        """Provider name, delegated to the wrapped provider."""

    @property
    def model_name(self) -> str:
        """Model name, delegated to the wrapped provider."""

    async def invoke(
        self,
        messages: list["Message"],
        tools: list["Tool"] | None = None,
        **kwargs: Any,
    ) -> "LLMResponse":
        """Execute the wrapped call, applying middleware behavior."""

    async def invoke_stream(
        self,
        messages: list["Message"],
        tools: list["Tool"] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator["Delta"]:
        """Stream from the wrapped provider, applying middleware behavior."""


# ---------------------------------------------------------------------------
# Arc-private kwargs filter
# ---------------------------------------------------------------------------

# kwargs injected by Arc layers (arcrun, arcagent) that backends must never see.
# These control Arc behavior but are not model input. Backends strip them before
# forwarding to their underlying provider.
ARC_PRIVATE_KWARGS = frozenset({
    "_arc_deadline",
    "tool_choice",
    "_retry_attempt",
    "_retry_group_id",
    "_queue_wait_ms",
    "_queue_job",
    "_queue_context",
    "lineage",
    "classification",
    "route",
})


def strip_arc_private_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Remove Arc-private kwargs before forwarding to a backend provider.

    Backends receive only kwargs that should influence the actual model call
    (response_format, temperature, max_tokens, etc.). Arc control plane
    fields are removed here.
    """
    return {k: v for k, v in kwargs.items() if k not in ARC_PRIVATE_KWARGS}


__all__ = [
    "ARC_PRIVATE_KWARGS",
    "Middleware",
    "strip_arc_private_kwargs",
]
