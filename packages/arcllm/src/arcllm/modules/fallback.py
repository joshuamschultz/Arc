"""FallbackModule — provider chain switching on failure."""

import logging
from typing import Any

from arcllm.exceptions import ArcLLMConfigError
from arcllm.modules.base import BaseModule
from arcllm.types import LLMProvider, LLMResponse, Message, Tool

logger = logging.getLogger(__name__)

_MAX_FALLBACK_CHAIN_LENGTH = 10


def load_model(provider: str) -> LLMProvider:
    """Lazy import to avoid circular dependency with registry."""
    from arcllm.registry import load_model as _load_model

    return _load_model(provider)


class FallbackModule(BaseModule):
    """Falls back to alternative providers when the primary fails.

    On any exception from the inner provider, walks a config-driven chain
    of provider names, creating each fallback adapter on-demand via
    load_model(). If all fallbacks also fail, raises the original
    (primary) error.

    Config keys:
        chain: List of provider names to try on failure (default: []).
    """

    def __init__(self, config: dict[str, Any], inner: LLMProvider) -> None:
        super().__init__(config, inner)
        self._chain: list[str] = config.get("chain", [])
        if len(self._chain) > _MAX_FALLBACK_CHAIN_LENGTH:
            raise ArcLLMConfigError(
                f"Fallback chain too long ({len(self._chain)} providers, "
                f"max {_MAX_FALLBACK_CHAIN_LENGTH})"
            )

    async def invoke(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        with self._span("arcllm.fallback") as fallback_span:
            try:
                return await self._inner.invoke(messages, tools, **kwargs)
            except Exception as primary_error:
                fallback_span.add_event(
                    "primary_failed", {"error": str(primary_error)}
                )
                logger.warning(
                    "Primary provider failed: %s. Trying %d fallback(s).",
                    primary_error,
                    len(self._chain),
                )
                for provider_name in self._chain:
                    fallback = None
                    with self._span("arcllm.fallback.attempt", attributes={"arcllm.fallback.provider": provider_name}):
                        try:
                            fallback = load_model(provider_name)
                            result = await fallback.invoke(messages, tools, **kwargs)
                            logger.info("Fallback to '%s' succeeded.", provider_name)
                            return result
                        except Exception as fallback_error:
                            logger.warning(
                                "Fallback '%s' failed: %s",
                                provider_name,
                                fallback_error,
                            )
                            continue
                        finally:
                            if fallback is not None and hasattr(fallback, "close"):
                                await fallback.close()
                logger.error(
                    "All %d fallbacks exhausted. Raising primary error.",
                    len(self._chain),
                )
                raise primary_error
