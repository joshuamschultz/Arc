"""SPEC-043 Phase F — ConcurrentStepExecutor + reserve-then-settle (T-F2/F3/F4).

The budget test is INTERLEAVING-FORCED: N branches run concurrently, forced to
overlap by an ``asyncio.Barrier`` inside the (mock) run so all are in-flight at
once. Reserve-then-settle bounds ``Σ spend ≤ Plan.budget`` and defers the branch
that would breach. A control test shows the pre-fix "read remaining_budget then
run" pattern OVERSPENDS under the same interleave — proving the guard, not the
mock (per feedback_concurrency_tests_must_interleave).
"""

from __future__ import annotations

import asyncio

import pytest

from arcagent.modules.planning.executor import ConcurrentStepExecutor, StepExecutor
from arcagent.modules.planning.models import (
    Plan,
    PlanBudget,
    PlanStatus,
    PlanStep,
    StepStatus,
)


def _plan(n_steps: int, *, max_tokens: int) -> Plan:
    return Plan(
        plan_id="p",
        goal="g",
        goal_source_did="did:arc:user",
        parent_goal_hash="h",
        status=PlanStatus.ACTIVE,
        steps=[PlanStep(step_id=f"s{i}", description=f"step {i}") for i in range(n_steps)],
        budget=PlanBudget(max_tokens=max_tokens),
    )


class _Usage:
    def __init__(self, total: int) -> None:
        self.total_tokens = total


class _Result:
    """Minimal LoopResult stand-in: a clean success spending ``spend`` tokens."""

    def __init__(self, spend: int) -> None:
        self.content = "ok"
        self.completion_payload = None
        self.completion_tool = None
        self.tokens_used = {"total": spend}
        self.cost_usd = 0.0


def _run_fn_spending_cap(barrier: asyncio.Barrier | None, want: int):
    async def run_fn(*, task: str, max_tokens, max_cost_usd, actor_did: str) -> _Result:
        if barrier is not None:
            try:
                await asyncio.wait_for(barrier.wait(), timeout=0.5)
            except (TimeoutError, asyncio.BrokenBarrierError):
                pass
        # A bounded run spends up to its cap; here it wants ``want`` but the
        # cap (reservation) clamps it — the branch cannot exceed its reservation.
        spend = want if max_tokens is None else min(want, max_tokens)
        return _Result(spend)

    return run_fn


class TestProtocolCompat:
    def test_satisfies_step_executor(self) -> None:
        ex = ConcurrentStepExecutor(_run_fn_spending_cap(None, 10))
        assert isinstance(ex, StepExecutor)


class TestReserveThenSettle:
    @pytest.mark.asyncio
    async def test_no_overspend_under_forced_interleave(self) -> None:
        # Budget 250, per-step cap 100, 4 ready branches, want 100 each.
        # Reserve: 100 + 100 + 50 (clamped) = 250; the 4th defers.
        plan = _plan(4, max_tokens=250)
        barrier = asyncio.Barrier(3)  # the 3 reservable branches overlap
        ex = ConcurrentStepExecutor(
            _run_fn_spending_cap(barrier, 100), max_parallel=10, step_max_tokens=100
        )
        outcomes = await ex.run_ready(plan.steps, plan=plan)
        # Only 3 branches could reserve budget; the 4th was deferred.
        assert len(outcomes) == 3
        # Σ spend never exceeds the aggregate ceiling (no N-way overspend).
        assert plan.tokens_spent <= 250
        # Reservations fully released after settle.
        assert plan.reserved_tokens == 0

    @pytest.mark.asyncio
    async def test_naive_no_reservation_overspends_control(self) -> None:
        """Control: the pre-fix read-then-run pattern DOES overspend (race real).

        Each branch reads ``remaining_budget`` (no reservation), all before any
        accrual (forced overlap via the barrier), so each sees the FULL budget
        and runs — total spend exceeds the ceiling. This is what reserve-then-
        settle prevents; it documents the window is real, not a mock artifact.
        """
        plan = _plan(4, max_tokens=250)
        barrier = asyncio.Barrier(4)
        spent = {"total": 0}

        async def naive_branch(step: PlanStep) -> None:
            rem_tok, _ = plan.remaining_budget()  # no reservation — the bug
            cap = 100 if rem_tok is None else min(100, rem_tok)
            try:
                await asyncio.wait_for(barrier.wait(), timeout=0.5)
            except (TimeoutError, asyncio.BrokenBarrierError):
                pass
            if cap > 0:
                spent["total"] += 100  # all four saw room and ran

        await asyncio.gather(*(naive_branch(s) for s in plan.steps))
        assert spent["total"] > 250  # OVERSPEND — the race the guard closes


class TestFailureIsolation:
    @pytest.mark.asyncio
    async def test_failing_branch_does_not_crash_siblings(self) -> None:
        plan = _plan(3, max_tokens=None)  # unbounded budget

        async def run_fn(*, task: str, max_tokens, max_cost_usd, actor_did: str) -> _Result:
            if task == "step 1":
                raise RuntimeError("branch 1 exploded")
            return _Result(5)

        ex = ConcurrentStepExecutor(run_fn)
        outcomes = await ex.run_ready(plan.steps, plan=plan)
        assert len(outcomes) == 3
        assert outcomes[0].status is StepStatus.SUCCEEDED
        assert outcomes[1].status is StepStatus.FAILED
        assert "exploded" in (outcomes[1].failure_reason or "")
        assert outcomes[2].status is StepStatus.SUCCEEDED


class TestCancellationReleasesReservation:
    @pytest.mark.asyncio
    async def test_cancelled_branch_releases_its_reservation(self) -> None:
        """A cancelled branch must still release its reservation (SPEC-043 F4).

        ``except Exception`` does NOT catch ``asyncio.CancelledError``, so before
        the fix a cancelled branch skipped ``_settle`` and its reservation leaked
        — the plan wedged under-budget forever. ``_settle`` now lives in a
        ``finally`` so cancellation releases exactly once.
        """
        plan = _plan(1, max_tokens=100)
        started = asyncio.Event()

        async def blocking_run_fn(*, task, max_tokens, max_cost_usd, actor_did):  # type: ignore[no-untyped-def]
            started.set()
            await asyncio.Event().wait()  # block forever until cancelled
            return _Result(0)

        ex = ConcurrentStepExecutor(blocking_run_fn, step_max_tokens=100)
        task = asyncio.ensure_future(ex.run_step(plan.steps[0], plan=plan))
        await started.wait()
        assert plan.reserved_tokens == 100  # reservation is held while in-flight
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The reservation was released despite the cancellation — no leak.
        assert plan.reserved_tokens == 0
        assert plan.reserved_cost == 0


class TestAvailableBudget:
    def test_available_shrinks_with_reservation(self) -> None:
        plan = _plan(1, max_tokens=100)
        assert plan.available_budget() == (100, None)
        plan.reserved_tokens = 40
        assert plan.available_budget() == (60, None)
        plan.tokens_spent = 30
        assert plan.available_budget() == (30, None)

    def test_dag_methods_unchanged(self) -> None:
        # ready_steps / validate_dag behave identically with the new fields.
        plan = _plan(2, max_tokens=100)
        plan.steps[1].depends_on = ["s0"]
        plan.validate_dag()
        assert [s.step_id for s in plan.ready_steps()] == ["s0"]
