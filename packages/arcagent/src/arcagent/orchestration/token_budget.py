"""Shared token-budget primitives for spawn orchestration.

Sibling of ``arcagent.orchestration.spawn``. Owns the cross-child token
pool that fixes the Hermes implicit-token-pool bug: children that debit
no shared budget silently spend multiple times the caller's allocation
with no warning.

Re-exported through ``arcagent.orchestration.spawn`` and
``arcagent.orchestration`` so existing imports
(``from arcagent.orchestration.spawn import RootTokenBudget, TokenUsage``,
 ``from arcagent.orchestration import RootTokenBudget``) keep working
unchanged.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel


class RootTokenBudget:
    """Shared token budget for a root run and all its spawned children.

    Thread-safety: asyncio-only. Uses asyncio.Lock for atomic debit.
    Not safe to use across multiple event loops.

    Args:
        total: Maximum tokens the root run and all children may consume.
               Must be positive.
    """

    def __init__(self, total: int) -> None:
        if total <= 0:
            raise ValueError(f"RootTokenBudget total must be positive, got {total}")
        self._total = total
        self._used = 0
        self._lock = asyncio.Lock()

    @property
    def total(self) -> int:
        """Total token budget."""
        return self._total

    @property
    def used(self) -> int:
        """Tokens consumed so far (pre-debits + record_actual overages)."""
        return self._used

    @property
    def remaining(self) -> int:
        """Tokens remaining; never goes negative."""
        return max(0, self._total - self._used)

    def is_exhausted(self) -> bool:
        """True when used >= total."""
        return self._used >= self._total

    async def try_debit(self, amount: int) -> bool:
        """Atomically debit *amount* from the budget.

        Returns True if the debit succeeded; False if the budget would be
        exceeded. On False, the budget is not modified.
        """
        async with self._lock:
            if self._used + amount > self._total:
                return False
            self._used += amount
            return True

    async def record_actual(self, amount: int) -> None:
        """Record actual token usage after a call completes.

        This may push used past total — that is intentional. In-flight children
        may return more tokens than the pre-debit estimate. record_actual
        corrects the accounting for audit purposes without blocking the child.
        """
        async with self._lock:
            self._used += amount


class TokenUsage(BaseModel):
    """Token usage counters for a single run."""

    input: int = 0
    output: int = 0
    total: int = 0
