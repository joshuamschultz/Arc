"""Unit tests for RootTokenBudget — atomic debit, exhaustion, in-flight propagation.

These tests directly validate the Hermes implicit-token-pool bug fix.
Hermes bug: children debit no shared root budget; 3 children x 50 iters each
silently spends 4x the caller's allocation with no warning.

This test suite proves:
1. Pre-debit refusal on over-budget
2. Atomic debit under concurrent pressure
3. in-flight exhaustion detection
4. record_actual for overage accounting
"""

from __future__ import annotations

import asyncio

import pytest

from arcagent.orchestration.spawn import RootTokenBudget


class TestDebitBehavior:
    @pytest.mark.asyncio
    async def test_single_debit_within_budget(self) -> None:
        budget = RootTokenBudget(total=1000)
        assert await budget.try_debit(999) is True
        assert budget.remaining == 1

    @pytest.mark.asyncio
    async def test_exact_budget_debit(self) -> None:
        budget = RootTokenBudget(total=100)
        assert await budget.try_debit(100) is True
        assert budget.remaining == 0
        assert budget.is_exhausted() is True

    @pytest.mark.asyncio
    async def test_over_budget_refused(self) -> None:
        budget = RootTokenBudget(total=100)
        assert await budget.try_debit(101) is False
        assert budget.used == 0  # not debited

    @pytest.mark.asyncio
    async def test_sequential_debits_accumulate(self) -> None:
        budget = RootTokenBudget(total=300)
        for _ in range(3):
            ok = await budget.try_debit(100)
            assert ok is True
        assert budget.used == 300
        assert budget.remaining == 0

    @pytest.mark.asyncio
    async def test_fourth_debit_refused_when_exhausted(self) -> None:
        budget = RootTokenBudget(total=300)
        for _ in range(3):
            await budget.try_debit(100)
        ok = await budget.try_debit(1)
        assert ok is False

    @pytest.mark.asyncio
    async def test_partial_debit_after_some_use(self) -> None:
        budget = RootTokenBudget(total=500)
        await budget.try_debit(300)
        ok = await budget.try_debit(200)
        assert ok is True
        assert budget.remaining == 0

    @pytest.mark.asyncio
    async def test_partial_debit_refused_when_would_exceed(self) -> None:
        budget = RootTokenBudget(total=500)
        await budget.try_debit(300)
        ok = await budget.try_debit(201)
        assert ok is False
        assert budget.used == 300  # unchanged


class TestExhaustionCheck:
    @pytest.mark.asyncio
    async def test_not_exhausted_at_start(self) -> None:
        budget = RootTokenBudget(total=100)
        assert budget.is_exhausted() is False

    @pytest.mark.asyncio
    async def test_exhausted_at_exact_limit(self) -> None:
        budget = RootTokenBudget(total=100)
        await budget.try_debit(100)
        assert budget.is_exhausted() is True

    @pytest.mark.asyncio
    async def test_not_exhausted_one_below_limit(self) -> None:
        budget = RootTokenBudget(total=100)
        await budget.try_debit(99)
        assert budget.is_exhausted() is False


class TestRecordActual:
    @pytest.mark.asyncio
    async def test_record_actual_adds_to_used(self) -> None:
        budget = RootTokenBudget(total=1000)
        await budget.try_debit(100)  # pre-debit estimate
        await budget.record_actual(50)  # actual overage
        assert budget.used == 150

    @pytest.mark.asyncio
    async def test_record_actual_can_push_past_budget(self) -> None:
        """In-flight children may exceed budget — record_actual allows this."""
        budget = RootTokenBudget(total=100)
        await budget.try_debit(90)
        await budget.record_actual(50)
        assert budget.used == 140
        assert budget.is_exhausted() is True

    @pytest.mark.asyncio
    async def test_record_actual_triggers_exhaustion_flag(self) -> None:
        budget = RootTokenBudget(total=100)
        await budget.try_debit(60)
        assert budget.is_exhausted() is False
        await budget.record_actual(41)
        assert budget.is_exhausted() is True


class TestConcurrentAtomicity:
    @pytest.mark.asyncio
    async def test_only_one_wins_tight_budget(self) -> None:
        """Classic Hermes bug: multiple children trying to debit same pool concurrently."""
        budget = RootTokenBudget(total=100)
        # Three children each request 80 tokens; only one should win
        results = await asyncio.gather(
            budget.try_debit(80),
            budget.try_debit(80),
            budget.try_debit(80),
        )
        assert results.count(True) == 1
        assert budget.used == 80

    @pytest.mark.asyncio
    async def test_concurrent_small_debits_sum_correctly(self) -> None:
        """Many small debits — all should succeed and sum correctly."""
        budget = RootTokenBudget(total=1000)
        results = await asyncio.gather(*[budget.try_debit(10) for _ in range(100)])
        assert all(results)
        assert budget.used == 1000

    @pytest.mark.asyncio
    async def test_concurrent_debits_exceed_budget_exactly_once(self) -> None:
        """Exactly N debits should succeed when N x amount == total."""
        budget = RootTokenBudget(total=500)
        results = await asyncio.gather(*[budget.try_debit(100) for _ in range(7)])
        successes = results.count(True)
        assert successes == 5  # exactly 5 x 100 = 500
        assert budget.used == 500


class TestPropertyAccess:
    def test_total_property(self) -> None:
        budget = RootTokenBudget(total=999)
        assert budget.total == 999

    @pytest.mark.asyncio
    async def test_remaining_property(self) -> None:
        budget = RootTokenBudget(total=200)
        await budget.try_debit(75)
        assert budget.remaining == 125

    @pytest.mark.asyncio
    async def test_used_property(self) -> None:
        budget = RootTokenBudget(total=500)
        await budget.try_debit(123)
        assert budget.used == 123

    @pytest.mark.asyncio
    async def test_remaining_never_negative(self) -> None:
        """remaining uses max(0, ...) so it never goes negative even with record_actual."""
        budget = RootTokenBudget(total=100)
        await budget.try_debit(100)
        await budget.record_actual(500)  # way over budget via actual
        assert budget.remaining == 0  # max(0, ...)
