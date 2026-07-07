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

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from arcrun.types import LoopResult

from arcagent.modules.planning.models import Plan, PlanStep, StepStatus

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


def build_arcrun_run_fn(
    *,
    model: Any,
    capabilities: Any,
    system_prompt: str,
    max_turns: int = 12,
) -> RunFn:
    """Bind a :data:`RunFn` to ``arcrun.run`` with policy-gated capabilities.

    ``capabilities`` is the agent's real ``CapabilityProvider`` (tools carry
    the ToolRegistry policy wrapper), so every tool call inside the step run is
    gated by the SPEC-034 pipeline. Forces the react strategy — one bounded
    reason-act-observe run per step.
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
        )

    return run_fn


__all__ = [
    "ArcRunStepExecutor",
    "RunFn",
    "StepExecutor",
    "StepOutcome",
    "build_arcrun_run_fn",
    "classify_loop_result",
]
