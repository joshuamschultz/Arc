"""Strategy interface and selection."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from arcrun.state import RunState

STRATEGIES: dict[str, Any] = {}


def _load_strategies() -> None:
    from arcrun.strategies.react import react_loop

    STRATEGIES["react"] = react_loop


async def select_strategy(
    allowed: list[str] | None,
    model: Any,
    state: RunState,
) -> str:
    """
    Single allowed -> use it.
    Multiple -> model picks on first turn (future).
    None -> default to 'react'.
    """
    if not STRATEGIES:
        _load_strategies()

    if allowed is None:
        return "react"
    unknown = [s for s in allowed if s not in STRATEGIES]
    if unknown:
        raise ValueError(f"unknown strategies: {unknown}. available: {list(STRATEGIES)}")
    if len(allowed) == 1:
        return allowed[0]
    return "react"
