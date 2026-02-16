"""TelemetryModule — structured logging of timing, tokens, and cost per invoke()."""

import logging
import time
from typing import Any

from arcllm.exceptions import ArcLLMConfigError
from arcllm.modules._logging import log_structured, validate_log_level
from arcllm.modules.base import BaseModule
from arcllm.types import LLMProvider, LLMResponse, Message, Tool, Usage

logger = logging.getLogger(__name__)

_VALID_CONFIG_KEYS = {
    "cost_input_per_1m",
    "cost_output_per_1m",
    "cost_cache_read_per_1m",
    "cost_cache_write_per_1m",
    "log_level",
    "enabled",
}


class TelemetryModule(BaseModule):
    """Wraps invoke() to log timing, token usage, and cost.

    Config keys:
        cost_input_per_1m: Cost per 1M input tokens (default: 0.0).
        cost_output_per_1m: Cost per 1M output tokens (default: 0.0).
        cost_cache_read_per_1m: Cost per 1M cache read tokens (default: 0.0).
        cost_cache_write_per_1m: Cost per 1M cache write tokens (default: 0.0).
        log_level: Python log level name (default: "INFO").
    """

    def __init__(self, config: dict[str, Any], inner: LLMProvider) -> None:
        super().__init__(config, inner)

        unknown = set(config.keys()) - _VALID_CONFIG_KEYS
        if unknown:
            raise ArcLLMConfigError(
                f"Unknown TelemetryModule config keys: {sorted(unknown)}. "
                f"Valid keys: {sorted(_VALID_CONFIG_KEYS - {'enabled'})}"
            )

        self._cost_input: float = config.get("cost_input_per_1m", 0.0)
        self._cost_output: float = config.get("cost_output_per_1m", 0.0)
        self._cost_cache_read: float = config.get("cost_cache_read_per_1m", 0.0)
        self._cost_cache_write: float = config.get("cost_cache_write_per_1m", 0.0)

        if self._cost_input < 0:
            raise ArcLLMConfigError("cost_input_per_1m must be >= 0")
        if self._cost_output < 0:
            raise ArcLLMConfigError("cost_output_per_1m must be >= 0")
        if self._cost_cache_read < 0:
            raise ArcLLMConfigError("cost_cache_read_per_1m must be >= 0")
        if self._cost_cache_write < 0:
            raise ArcLLMConfigError("cost_cache_write_per_1m must be >= 0")

        self._log_level: int = validate_log_level(config)

    def _calculate_cost(self, usage: Usage) -> float:
        """Calculate USD cost from token counts and per-1M pricing."""
        cost = (
            usage.input_tokens * self._cost_input
            + usage.output_tokens * self._cost_output
        ) / 1_000_000

        if usage.cache_read_tokens:
            cost += usage.cache_read_tokens * self._cost_cache_read / 1_000_000
        if usage.cache_write_tokens:
            cost += usage.cache_write_tokens * self._cost_cache_write / 1_000_000

        return cost

    async def invoke(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        with self._span("arcllm.telemetry") as tel_span:
            start = time.monotonic()
            response = await self._inner.invoke(messages, tools, **kwargs)
            elapsed = time.monotonic() - start

            usage = response.usage
            cost = self._calculate_cost(usage)
            duration_ms = round(elapsed * 1000, 1)

            tel_span.set_attribute("arcllm.telemetry.duration_ms", duration_ms)
            tel_span.set_attribute("arcllm.telemetry.cost_usd", cost)

            log_structured(
                logger,
                self._log_level,
                "LLM call",
                provider=self._inner.name,
                model=response.model,
                duration_ms=duration_ms,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cost_usd=cost,
                stop_reason=response.stop_reason,
            )

            return response
