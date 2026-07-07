"""Plan-Execute control loop — a deterministic DAG walk (SPEC-040).

This is plan *orchestration*, not an agentic loop: no LLM turn per iteration.
It marks a ready step RUNNING, drives it through the injected ``StepExecutor``
(one bounded arcrun run), applies the outcome, and checkpoints the plan before
proceeding (REQ-011). On a step failure it revises the remainder via the
injected ``replan_fn`` (arcllm), bounded by ``max_replans`` so it can never run
away (REQ-031). One ready step is executed at a time — the DAG *expresses*
parallelism, but concurrent dispatch is deferred to SPEC-043's arcrun strategy
behind the same ``StepExecutor`` seam (OQ-2).

Resume (REQ-012): :meth:`resume` reloads the ACTIVE plan and re-enters the
walk; ``ready_steps`` re-derives the frontier from ``depends_on`` + the
SUCCEEDED set, so already-completed work is skipped without a stored cursor.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from arcagent.modules.planning.executor import StepExecutor, StepOutcome
from arcagent.modules.planning.models import Plan, PlanStatus, PlanStep, StepStatus
from arcagent.modules.planning.store import PlanStore

_logger = logging.getLogger("arcagent.modules.planning.orchestrator")

# Revise the remaining plan around a failure; returns a new ACTIVE plan.
ReplanFn = Callable[[Plan, str], Awaitable[Plan]]


def _exhaustion_terminator(plan: Plan) -> dict[str, object]:
    """Structured terminator mirroring arcrun's ``make_budget_breach_args`` shape."""
    return {
        "status": "failed",
        "summary": f"replan budget exhausted after {plan.replans_used} revisions",
        "error": "max_replans",
    }


class PlanOrchestrator:
    """Walks a plan DAG to a terminal state, checkpointing every transition."""

    def __init__(
        self,
        store: PlanStore,
        executor: StepExecutor,
        *,
        replan_fn: ReplanFn,
    ) -> None:
        self._store = store
        self._executor = executor
        self._replan_fn = replan_fn

    async def execute(self, plan: Plan) -> Plan:
        """Drive ``plan`` (already persisted ACTIVE) to COMPLETED or FAILED.

        When the injected executor advertises a concurrent ``run_ready`` batch
        method (a :class:`ConcurrentStepExecutor`), the whole ready frontier is
        dispatched concurrently with reserve-then-settle (SPEC-043 REQ-053/056);
        otherwise the interim sequential ``run_step`` path runs one step at a time
        (SPEC-040). The ``Plan`` model + ``ready_steps`` are identical either way —
        only the injected executor differs (REQ-054).
        """
        run_ready = getattr(self._executor, "run_ready", None)
        while not plan.is_terminal():
            if plan.budget_exhausted():
                break  # aggregate LLM10 ceiling hit — finalize as FAILED
            ready = plan.ready_steps()
            if not ready:
                break
            if run_ready is not None:
                failure = await self._run_frontier(plan, ready, run_ready)
            else:
                failure = await self._run_sequential(plan, ready[0])
            if failure is not None:
                # A budget-exhausted failure is terminal — never spend a replan
                # (and its LLM call) chasing a plan that has no budget left.
                if plan.budget_exhausted() or plan.replans_used >= plan.max_replans:
                    break  # finalize as FAILED with a terminator
                plan = await self._replan(plan, failure)
        return self._finalize(plan)

    async def _run_sequential(self, plan: Plan, step: PlanStep) -> str | None:
        """Run one ready step; return a failure reason to replan on, else None."""
        await self._run_one(plan, step)
        return step.failure_reason if step.status is StepStatus.FAILED else None

    async def _run_frontier(
        self, plan: Plan, ready: list[PlanStep], run_ready: Any
    ) -> str | None:
        """Dispatch the whole ready frontier concurrently, checkpoint each result.

        The executor reserves each branch's budget, runs the reserved branches
        concurrently (reserve-then-settle keeps ``Σ ≤ Plan.budget``), and returns
        per-branch outcomes; a failing branch is isolated (REQ-055). Returns the
        first failure reason (to trigger a replan) or None.
        """
        for step in ready:
            step.status = StepStatus.RUNNING
        self._store.save(plan, action="plan.frontier.started")
        outcomes = await run_ready(ready, plan=plan)
        first_failure: str | None = None
        for step, outcome in zip(ready, outcomes, strict=False):
            self._apply_outcome(plan, step, outcome)
            if outcome.status is StepStatus.FAILED and first_failure is None:
                first_failure = outcome.failure_reason or "step failed"
        # Steps that could not reserve budget this pass were deferred (still
        # RUNNING marker on non-run steps) — reset them to PENDING for next pass.
        for step in ready[len(outcomes) :]:
            step.status = StepStatus.PENDING
        return first_failure

    def _apply_outcome(self, plan: Plan, step: PlanStep, outcome: StepOutcome) -> None:
        """Commit one branch outcome onto the plan + checkpoint (REQ-011/055)."""
        if outcome.status is StepStatus.SUCCEEDED:
            step.status = StepStatus.SUCCEEDED
            step.result = outcome.result
            self._store.save(plan, action="plan.step.succeeded", target=step.step_id)
        else:
            step.status = StepStatus.FAILED
            step.failure_reason = outcome.failure_reason
            self._store.save(
                plan,
                action="plan.step.failed",
                target=step.step_id,
                outcome="error",
                extra={"reason": outcome.failure_reason},
            )

    async def resume(self) -> Plan | None:
        """Resume the ACTIVE plan for this workspace, if one exists (REQ-012)."""
        plan = self._store.active_plan()
        if plan is None:
            return None
        # A step caught mid-flight by a crash is retried, not left stuck.
        for step in plan.steps:
            if step.status is StepStatus.RUNNING:
                step.status = StepStatus.PENDING
        return await self.execute(plan)

    async def _run_one(self, plan: Plan, step: PlanStep) -> None:
        """Execute one ready step and checkpoint before/after (REQ-011)."""
        step.status = StepStatus.RUNNING
        self._store.save(plan, action="plan.step.started", target=step.step_id)
        outcome = await self._executor.run_step(step, plan=plan)
        # Accrue actual consumption onto the durable aggregate before any
        # branch so the running total is checkpointed with the plan (REQ-022).
        plan.tokens_spent += outcome.tokens_used
        plan.cost_spent += outcome.cost_usd
        if outcome.status is StepStatus.SUCCEEDED:
            step.status = StepStatus.SUCCEEDED
            step.result = outcome.result
            self._store.save(plan, action="plan.step.succeeded", target=step.step_id)
        else:
            step.status = StepStatus.FAILED
            step.failure_reason = outcome.failure_reason
            self._store.save(
                plan,
                action="plan.step.failed",
                target=step.step_id,
                outcome="error",
                extra={"reason": outcome.failure_reason},
            )

    async def _replan(self, plan: Plan, reason: str) -> Plan:
        """Revise the remainder (arcllm) and audit the version delta (REQ-032)."""
        revised = await self._replan_fn(plan, reason)
        self._store.save(
            revised,
            action="plan.replanned",
            extra={
                "trigger": reason,
                "from_version": plan.version,
                "to_version": revised.version,
            },
        )
        return revised

    def _finalize(self, plan: Plan) -> Plan:
        """Emit the terminal event exactly once."""
        plan.status = plan.terminal_status()
        if plan.status is PlanStatus.COMPLETED:
            self._store.save(plan, action="plan.completed")
        else:
            self._store.save(
                plan,
                action="plan.failed",
                outcome="error",
                extra={"terminator": _exhaustion_terminator(plan)},
            )
        return plan


__all__ = ["PlanOrchestrator", "ReplanFn"]
