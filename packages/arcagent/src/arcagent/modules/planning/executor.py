"""The ``StepExecutor`` seam — arcagent drives, arcrun executes (SPEC-040).

A step runs by driving **one bounded arcrun run** (react strategy) through
the existing loop. The planner never dispatches a tool itself: every tool a
step invokes passes through ``ToolRegistry`` → ``PolicyPipeline`` (first-DENY
-wins, fail-closed) and the SPEC-038 budget breaker automatically, because it
is a real arcrun run (REQ-021/022). A DENY, a budget breach, or a tool error
is captured as a ``FAILED`` :class:`StepOutcome` with its reason — the planner
never crashes on a bad step (REQ-023).

The ``StepExecutor`` Protocol is the swap point: SPEC-043 satisfies the same
Protocol with a native arcrun Plan-Execute strategy that walks the DAG with
parallel dispatch. The plan model and orchestrator do not change — only which
executor is injected (REQ-020/025).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from arcrun.strategies.plan_execute import PlanExecuteStrategy
from arcrun.types import LoopResult

from arcagent.modules.planning.models import Plan, PlanStep, StepStatus

# A budget grant: the (tokens, cost) ceiling reserved for one branch. ``None`` on
# a dimension means unbounded (that dimension has no plan ceiling).
BudgetGrant = tuple["int | None", "float | None"]

# A callable that drives ONE bounded arcrun run and returns its LoopResult.
# Production binds this to ``arcrun.run`` with the agent's policy-gated
# capabilities + model; tests bind it to a real run or a scripted result.
RunFn = Callable[..., Awaitable[LoopResult]]


@dataclass
class StepOutcome:
    """Result of executing a single step: SUCCEEDED (+result) or FAILED (+reason).

    ``tokens_used`` / ``cost_usd`` report what the step's run actually consumed
    so the orchestrator can accumulate the plan aggregate (REQ-022, LLM10).
    """

    status: StepStatus
    result: str | None = None
    failure_reason: str | None = None
    tokens_used: int = 0
    cost_usd: float = 0.0


@runtime_checkable
class StepExecutor(Protocol):
    """Executes exactly one ready step to a terminal outcome."""

    async def run_step(self, step: PlanStep, *, plan: Plan) -> StepOutcome: ...


def classify_loop_result(result: LoopResult) -> StepOutcome:
    """Map an arcrun ``LoopResult`` to a step outcome (REQ-023).

    Discriminator (per arcrun's ``_build_result``): a synthesized budget/turn
    breach sets ``completion_payload`` but leaves ``completion_tool`` None; a
    real terminator tool sets both. A clean end leaves both None.
    """
    payload = result.completion_payload
    if payload is None:
        return StepOutcome(StepStatus.SUCCEEDED, result=result.content or "")
    if result.completion_tool is None:
        # Loop-synthesized breach (token / cost / turns) — never retried.
        reason = payload.get("error") or payload.get("summary") or "budget breach"
        return StepOutcome(StepStatus.FAILED, failure_reason=f"budget breach: {reason}")
    if payload.get("status") == "success":
        summary = payload.get("summary") or result.content or ""
        return StepOutcome(StepStatus.SUCCEEDED, result=summary)
    reason = payload.get("summary") or payload.get("error") or "step reported failure"
    return StepOutcome(StepStatus.FAILED, failure_reason=reason)


class ArcRunStepExecutor:
    """Interim executor: one bounded arcrun run per step.

    ``run_fn`` is the arcrun run seam (kwargs: ``task``, ``max_tokens``,
    ``max_cost_usd``, ``actor_did``). Each step's run is capped at the plan's
    *remaining* aggregate budget, so no single step — and no sum of steps —
    can exceed ``Plan.budget`` (REQ-022, LLM10). The step's actual consumption
    is reported back on the :class:`StepOutcome` so the orchestrator can
    accumulate the running total.
    """

    def __init__(self, run_fn: RunFn, *, actor_did: str = "") -> None:
        self._run_fn = run_fn
        self._actor_did = actor_did

    async def run_step(self, step: PlanStep, *, plan: Plan) -> StepOutcome:
        """Drive one bounded run for ``step``; capture any failure, never crash."""
        step.attempts += 1
        max_tokens, max_cost = plan.remaining_budget()
        if (max_tokens is not None and max_tokens <= 0) or (
            max_cost is not None and max_cost <= 0
        ):
            return StepOutcome(
                StepStatus.FAILED, failure_reason="plan budget exhausted"
            )
        try:
            result = await self._run_fn(
                task=step.description,
                max_tokens=max_tokens,
                max_cost_usd=max_cost,
                actor_did=self._actor_did,
            )
        except Exception as exc:  # reason: a bad step must not kill the planner
            return StepOutcome(StepStatus.FAILED, failure_reason=f"run error: {exc}")
        outcome = classify_loop_result(result)
        outcome.tokens_used = int(result.tokens_used.get("total", 0) or 0)
        outcome.cost_usd = result.cost_usd or 0.0
        return outcome


class ConcurrentStepExecutor:
    """Concurrent Plan-Execute executor — SPEC-043 REQ-053/054/055/056.

    Runs the whole ready frontier concurrently while holding the plan aggregate
    budget with **reserve-then-settle**: each branch reserves its cap from the
    shared budget BEFORE launch (under a plan-level lock) and settles actual spend
    on completion, so ``Σ(reservations + spend) ≤ Plan.budget`` — N concurrent
    branches can never overspend (LLM10). Concurrency reuses the ONE wired
    primitive (arcrun ``PlanExecuteStrategy.run_ready`` → ``ParallelDispatcher``),
    not a second gather path (REQ-056). A failing branch is captured as a
    ``FAILED`` outcome and never aborts a sibling (REQ-055). Satisfies the
    SPEC-040 ``StepExecutor`` seam (``run_step``) so it swaps ``ArcRunStepExecutor``
    by injection with **zero Plan-model change** (REQ-054).
    """

    def __init__(
        self,
        run_fn: RunFn,
        *,
        actor_did: str = "",
        max_parallel: int = 8,
        step_max_tokens: int | None = None,
        step_max_cost: float | None = None,
    ) -> None:
        self._run_fn = run_fn
        self._actor_did = actor_did
        self._max_parallel = max_parallel
        self._step_max_tokens = step_max_tokens
        self._step_max_cost = step_max_cost
        self._budget_lock = asyncio.Lock()
        self._dispatcher = PlanExecuteStrategy()

    async def run_step(self, step: PlanStep, *, plan: Plan) -> StepOutcome:
        """Run ONE step bounded by the plan's available budget (Protocol compat)."""
        grant = await self._reserve(plan)
        if grant is None:
            return StepOutcome(StepStatus.FAILED, failure_reason="plan budget exhausted")
        outcome = await self._run_branch(step, plan, grant)
        return outcome

    async def run_ready(self, steps: list[PlanStep], *, plan: Plan) -> list[StepOutcome]:
        """Dispatch the independent ready frontier concurrently, gated + bounded.

        Reservations are placed sequentially (each sees prior decrements) so the
        aggregate can never be over-committed; a step that cannot reserve any
        headroom is DEFERRED (left PENDING for a later pass). The reserved
        branches then run concurrently through the wired primitive and settle on
        completion. Returns outcomes for the steps that RAN, in submission order.
        """
        reserved: list[tuple[PlanStep, BudgetGrant]] = []
        for step in steps:
            grant = await self._reserve(plan)
            if grant is None:
                break  # no headroom this pass — defer the rest (stay PENDING)
            step.attempts += 1
            reserved.append((step, grant))
        if not reserved:
            return []

        async def _run(pair: tuple[PlanStep, BudgetGrant]) -> StepOutcome:
            branch_step, branch_grant = pair
            return await self._run_branch(branch_step, plan, branch_grant)

        return await self._dispatcher.run_ready(reserved, _run, max_parallel=self._max_parallel)

    async def _run_branch(
        self, step: PlanStep, plan: Plan, grant: BudgetGrant
    ) -> StepOutcome:
        """Run one bounded branch capped at its reservation, then settle (REQ-055).

        ``_settle`` runs in a ``finally`` so the reservation is released exactly
        once on EVERY exit — success, a captured error, OR cancellation (SPEC-043
        F4). ``except Exception`` does not catch ``CancelledError``; without the
        ``finally`` a cancelled branch would leak its reservation and wedge the
        plan under-budget forever.
        """
        max_tokens, max_cost = grant
        outcome = StepOutcome(StepStatus.FAILED, failure_reason="cancelled")
        try:
            result = await self._run_fn(
                task=step.description,
                max_tokens=max_tokens,
                max_cost_usd=max_cost,
                actor_did=self._actor_did,
            )
            outcome = classify_loop_result(result)
            outcome.tokens_used = int(result.tokens_used.get("total", 0) or 0)
            outcome.cost_usd = result.cost_usd or 0.0
        except Exception as exc:  # reason: a bad branch must not kill siblings
            outcome = StepOutcome(StepStatus.FAILED, failure_reason=f"run error: {exc}")
        finally:
            await self._settle(plan, grant, outcome)
        return outcome

    async def _reserve(self, plan: Plan) -> BudgetGrant | None:
        """Reserve this branch's cap from the shared budget (under the lock).

        Returns ``None`` when no headroom remains (defer/fail the branch); else
        grants ``min(per-step cap, available)`` on each bounded dimension and
        records the reservation so a later reserve sees less (REQ-053).
        """
        async with self._budget_lock:
            avail_tok, avail_cost = plan.available_budget()
            if (avail_tok is not None and avail_tok <= 0) or (
                avail_cost is not None and avail_cost <= 0
            ):
                return None
            grant_tok = self._cap(avail_tok, self._step_max_tokens)
            grant_cost = self._cap(avail_cost, self._step_max_cost)
            plan.reserved_tokens += grant_tok or 0
            plan.reserved_cost += grant_cost or 0
            return (grant_tok, grant_cost)

    async def _settle(self, plan: Plan, grant: BudgetGrant, outcome: StepOutcome) -> None:
        """Accrue actual spend and free the reservation (under the lock)."""
        async with self._budget_lock:
            plan.tokens_spent += outcome.tokens_used
            plan.cost_spent += outcome.cost_usd
            plan.reserved_tokens -= grant[0] or 0
            plan.reserved_cost -= grant[1] or 0

    @staticmethod
    def _cap(available: Any, per_step: Any) -> Any:
        """min(per-step ceiling, available); unbounded when both are ``None``."""
        if available is None:
            return per_step
        if per_step is None:
            return available
        return min(per_step, available)


def build_arcrun_run_fn(
    *,
    model: Any,
    capabilities: Any,
    system_prompt: str,
    max_turns: int = 12,
    approval_provider: Callable[..., Any] | None = None,
    approval_required_tools: frozenset[str] = frozenset(),
    max_repeat: int | None = None,
    max_consecutive_errors: int | None = None,
    on_checkpoint: Callable[[Any], None] | None = None,
    max_parallel: int = 10,
) -> RunFn:
    """Bind a :data:`RunFn` to ``arcrun.run`` with policy-gated capabilities.

    ``capabilities`` is the agent's real ``CapabilityProvider`` (tools carry
    the ToolRegistry policy wrapper), so every tool call inside the step run is
    gated by the SPEC-034 pipeline. Forces the react strategy — one bounded
    reason-act-observe run per step.

    The SPEC-043 loop controls — the resolved proactive-HITL approval set + its
    provider, the runaway/cascade breaker floors, and the checkpoint hook — are
    forwarded so a plan-step run is gated IDENTICALLY to the main loop (F2). The
    controls live in the returned closure, so both ``ArcRunStepExecutor`` and
    ``ConcurrentStepExecutor`` inherit them: a federal-flagged tool inside a plan
    branch pauses for approval and fails closed with no grant, never runs
    un-gated.
    """
    from arcrun import run as arcrun_run

    async def run_fn(
        *,
        task: str,
        max_tokens: int | None,
        max_cost_usd: float | None,
        actor_did: str,
    ) -> LoopResult:
        return await arcrun_run(
            model,
            capabilities,
            system_prompt,
            task,
            max_turns=max_turns,
            allowed_strategies=["react"],
            max_tokens=max_tokens,
            max_cost_usd=max_cost_usd,
            actor_did=actor_did or None,
            approval_provider=approval_provider,
            approval_required_tools=approval_required_tools,
            max_repeat=max_repeat,
            max_consecutive_errors=max_consecutive_errors,
            on_checkpoint=on_checkpoint,
            max_parallel=max_parallel,
        )

    return run_fn


__all__ = [
    "ArcRunStepExecutor",
    "BudgetGrant",
    "ConcurrentStepExecutor",
    "RunFn",
    "StepExecutor",
    "StepOutcome",
    "build_arcrun_run_fn",
    "classify_loop_result",
]
