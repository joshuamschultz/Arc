"""RoutingModule — classification-based provider routing.

Routes invoke() calls to different provider+model adapters based on a
``classification`` kwarg. Replaces the single adapter at the innermost
stack position when routing is enabled.
"""

import logging
import re
from typing import Any

from opentelemetry import trace

from arcllm.exceptions import ArcLLMConfigError
from arcllm.types import LLMProvider, LLMResponse, Message, Tool

logger = logging.getLogger(__name__)

# Classification strings follow the same safety rules as budget scopes:
# lowercase alphanumeric + underscores/colons/dots/hyphens, max 128 chars.
_CLASSIFICATION_RE = re.compile(r"^[a-z][a-z0-9_:.\-]{0,127}$")


class RoutingModule(LLMProvider):
    """Routes invoke() calls to provider+model based on classification kwarg.

    The classification kwarg flows through the module stack via ``**kwargs``
    and is consumed (popped) by this module. All other kwargs pass through
    to the selected adapter.

    Args:
        config: Routing configuration dict with ``enforcement`` and
            ``default_classification`` keys.
        adapters: Pre-built adapter instances keyed by classification name.
    """

    def __init__(
        self,
        config: dict[str, Any],
        adapters: dict[str, LLMProvider],
    ) -> None:
        self._enforcement: str = config.get("enforcement", "block")
        if self._enforcement not in ("warn", "block"):
            raise ArcLLMConfigError(
                f"enforcement must be 'warn' or 'block', got '{self._enforcement}'"
            )

        if not adapters:
            raise ArcLLMConfigError("RoutingModule requires at least one adapter")

        self._default_classification: str = config.get("default_classification", "unclassified")
        if self._default_classification not in adapters:
            raise ArcLLMConfigError(
                f"default_classification '{self._default_classification}' "
                f"not found in adapters: {sorted(adapters.keys())}"
            )

        self._adapters = dict(adapters)  # Defensive copy — prevent post-init mutation
        self._tracer = trace.get_tracer("arcllm")

    @property
    def name(self) -> str:
        """Return the default route's provider name."""
        return self._adapters[self._default_classification].name

    @property
    def model_name(self) -> str:
        """Return the default route's model name."""
        return self._adapters[self._default_classification].model_name

    async def invoke(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Route to the adapter matching the classification kwarg."""
        classification = kwargs.pop("classification", self._default_classification)

        # Validate format — reject injection attempts before lookup
        if not _CLASSIFICATION_RE.match(classification):
            raise ArcLLMConfigError(
                "Invalid classification format. Must be lowercase alphanumeric "
                "with underscores, colons, dots, or hyphens, max 128 characters."
            )

        with self._tracer.start_as_current_span("arcllm.routing") as span:
            span.set_attribute("arcllm.routing.classification", classification)
            span.set_attribute("arcllm.routing.enforcement", self._enforcement)

            adapter = self._adapters.get(classification)
            if adapter is None:
                if self._enforcement == "block":
                    raise ArcLLMConfigError(
                        f"Unknown classification '{classification}'. No matching route configured."
                    )
                # Warn mode — fall back to default
                logger.warning(
                    "Unknown classification '%s', routing to default '%s'",
                    classification,
                    self._default_classification,
                )
                adapter = self._adapters[self._default_classification]
                span.set_attribute("arcllm.routing.action", "defaulted")
            else:
                span.set_attribute("arcllm.routing.action", "routed")

            span.set_attribute("arcllm.routing.selected_provider", adapter.name)
            span.set_attribute("arcllm.routing.selected_model", adapter.model_name)

            return await adapter.invoke(messages, tools, **kwargs)

    def validate_config(self) -> bool:
        """All adapters must be valid."""
        return all(a.validate_config() for a in self._adapters.values())

    async def close(self) -> None:
        """Close ALL internal adapters, tolerating individual failures."""
        errors: list[Exception] = []
        for name, adapter in self._adapters.items():
            try:
                await adapter.close()
            except Exception as exc:
                logger.error("Failed to close adapter '%s': %s", name, exc)
                errors.append(exc)
        if errors:
            raise ExceptionGroup("Failed to close some adapters", errors)
