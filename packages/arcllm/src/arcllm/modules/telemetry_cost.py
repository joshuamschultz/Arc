"""Cost calculation helpers for TelemetryModule.

Pure functions that compute USD cost from token counts and per-1M
pricing. Extracted from TelemetryModule so the math is independently
testable and can be reused by ad-hoc cost-tracking tools.
"""

from __future__ import annotations

from arcllm.types import Usage

# Fallback for pre-flight cost estimation when ``max_tokens`` not passed
# by the caller.
DEFAULT_MAX_TOKENS = 4096


def calculate_cost(
    usage: Usage,
    *,
    input_per_1m: float,
    output_per_1m: float,
    cache_read_per_1m: float = 0.0,
    cache_write_per_1m: float = 0.0,
) -> float:
    """Calculate USD cost from token counts and per-1M pricing."""
    cost = (usage.input_tokens * input_per_1m + usage.output_tokens * output_per_1m) / 1_000_000

    if usage.cache_read_tokens:
        cost += usage.cache_read_tokens * cache_read_per_1m / 1_000_000
    if usage.cache_write_tokens:
        cost += usage.cache_write_tokens * cache_write_per_1m / 1_000_000

    return cost


def estimate_cost(max_tokens: int, *, output_per_1m: float) -> float:
    """Estimate worst-case cost using max_tokens * output price."""
    return max_tokens * output_per_1m / 1_000_000
