"""SPEC-043 Phase F — plan_execute strategy (concurrent independent items)."""

from __future__ import annotations

import asyncio

import pytest

from arcrun.strategies import STRATEGIES, select_strategy
from arcrun.strategies.plan_execute import PlanExecuteStrategy


class TestPlanExecuteStrategy:
    def test_registered_in_strategies(self) -> None:
        if not STRATEGIES:
            from arcrun.strategies import _load_strategies

            _load_strategies()
        assert "plan_execute" in STRATEGIES
        assert isinstance(STRATEGIES["plan_execute"], PlanExecuteStrategy)

    @pytest.mark.asyncio
    async def test_runs_items_concurrently_in_order(self) -> None:
        strat = PlanExecuteStrategy()
        order_started: list[int] = []

        async def runner(item: int) -> str:
            order_started.append(item)
            await asyncio.sleep(0.05)
            return f"out:{item}"

        import time

        start = time.monotonic()
        outcomes = await strat.run_ready([0, 1, 2], runner, max_parallel=10)
        elapsed = time.monotonic() - start
        # Concurrent: ~0.05s, not ~0.15s sequential.
        assert elapsed < 0.12
        # Submission order preserved regardless of completion order.
        assert outcomes == ["out:0", "out:1", "out:2"]

    @pytest.mark.asyncio
    async def test_failure_isolated_per_item(self) -> None:
        strat = PlanExecuteStrategy()

        async def runner(item: int) -> str:
            if item == 1:
                raise ValueError("branch 1 failed")
            return f"out:{item}"

        outcomes = await strat.run_ready([0, 1, 2], runner)
        assert outcomes[0] == "out:0"
        assert isinstance(outcomes[1], ValueError)  # captured, not raised
        assert outcomes[2] == "out:2"

    @pytest.mark.asyncio
    async def test_empty_batch(self) -> None:
        strat = PlanExecuteStrategy()
        assert await strat.run_ready([], lambda x: x) == []

    @pytest.mark.asyncio
    async def test_never_selected_crashes(self) -> None:
        """plan_execute is a valid registered strategy name (selectable)."""
        name = await select_strategy(["plan_execute"], model=None, state=None)
        assert name == "plan_execute"
