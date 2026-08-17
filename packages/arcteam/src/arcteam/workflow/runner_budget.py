"""Run-level budget accounting: reserve, then settle (SPEC-061 REQ-236).

This pattern was ported from arcagent's (since-removed) planning module,
which pioneered reserve-then-settle accounting for concurrent budget branches.
``arcteam`` sits below ``arcagent`` and may not import it, so the accounting
is reimplemented here, not shared: same admission rule, same lock discipline,
same ``min(per-item cap, available)`` grant, so a workflow node spends a
budget the same way that module's plan steps did.

Why reserve at all: without it, N nodes admitted in one tick each check the same
headroom and collectively overshoot. A reservation is subtracted from what the
next admission can see, so the (N+1)-th node that would breach gets nothing and
is DEFERRED — deferral, not failure, because the reservation is released on
settle and the node is admitted on a later tick.
"""

from __future__ import annotations

import asyncio
from typing import TypeVar

BudgetGrant = tuple[int | None, float | None]
"""A per-node allowance on each bounded dimension; ``None`` means unbounded."""

_N = TypeVar("_N", int, float)


def _cap(available: _N | None, per_node: _N | None) -> _N | None:
    """``min(per-node ceiling, available)``; unbounded when both are ``None``."""
    if available is None:
        return per_node
    if per_node is None:
        return available
    return min(per_node, available)


class RunBudget:
    """Token and cost accounting for one run, shared by its concurrent nodes."""

    def __init__(
        self,
        *,
        max_tokens: int | None = None,
        max_cost_usd: float | None = None,
        tokens_spent: int = 0,
        cost_spent: float = 0.0,
        reserved_tokens: int = 0,
        reserved_cost: float = 0.0,
    ) -> None:
        self.max_tokens = max_tokens
        self.max_cost_usd = max_cost_usd
        self.tokens_spent = tokens_spent
        self.cost_spent = cost_spent
        self.reserved_tokens = reserved_tokens
        self.reserved_cost = reserved_cost
        self._lock = asyncio.Lock()

    def available_budget(self) -> tuple[int | None, float | None]:
        """Headroom available to RESERVE now: remaining minus outstanding grants.

        Floored at zero; an unbounded dimension stays ``None``.
        """
        return (
            None
            if self.max_tokens is None
            else max(0, self.max_tokens - self.tokens_spent - self.reserved_tokens),
            None
            if self.max_cost_usd is None
            else max(0.0, self.max_cost_usd - self.cost_spent - self.reserved_cost),
        )

    def exhausted(self) -> str | None:
        """The dimension whose ceiling actual spend has reached, or ``None``."""
        if self.max_tokens is not None and self.tokens_spent >= self.max_tokens:
            return "tokens"
        if self.max_cost_usd is not None and self.cost_spent >= self.max_cost_usd:
            return "cost"
        return None

    async def reserve(
        self, *, per_node_tokens: int | None, per_node_cost: float | None
    ) -> BudgetGrant | None:
        """Reserve one node's allowance. ``None`` means no headroom — defer it."""
        async with self._lock:
            available_tokens, available_cost = self.available_budget()
            if (available_tokens is not None and available_tokens <= 0) or (
                available_cost is not None and available_cost <= 0
            ):
                return None
            grant_tokens = _cap(available_tokens, per_node_tokens)
            grant_cost = _cap(available_cost, per_node_cost)
            self.reserved_tokens += grant_tokens or 0
            self.reserved_cost += grant_cost or 0.0
            return (grant_tokens, grant_cost)

    async def settle(self, grant: BudgetGrant, *, tokens_used: int, cost_usd: float) -> None:
        """Accrue actual spend and release the reservation."""
        async with self._lock:
            self.tokens_spent += tokens_used
            self.cost_spent += cost_usd
            self.reserved_tokens -= grant[0] or 0
            self.reserved_cost -= grant[1] or 0.0


__all__ = ["BudgetGrant", "RunBudget"]
