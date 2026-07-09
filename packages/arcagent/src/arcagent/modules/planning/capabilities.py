"""Planner LLM surface + hooks — SPEC-021 capabilities (SPEC-040).

Four tools expose the plan lifecycle to the agent (REQ-042):

  * ``plan_create``  — decompose a goal into a DAG and execute it end to end.
  * ``plan_status``  — report the active plan and its step statuses (read-only).
  * ``plan_replan``  — force a bounded revision of the remaining plan.
  * ``plan_abandon`` — abandon the active plan.

Two hooks wire it into the agent:

  * ``agent:assemble_prompt`` (prio 60) injects the active plan frontier,
    replacing the old pending-tasks injection (REQ-041).
  * ``agent:ready`` binds the agent run seam so steps drive real, policy- and
    budget-gated arcrun runs.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import uuid4

from arcrun.types import LoopResult

from arcagent.modules.planning import _runtime
from arcagent.modules.planning.decomposer import DecompositionError, decompose, replan
from arcagent.modules.planning.executor import (
    ArcRunStepExecutor,
    ConcurrentStepExecutor,
    StepExecutor,
)
from arcagent.modules.planning.models import Plan, PlanStatus
from arcagent.modules.planning.orchestrator import PlanOrchestrator
from arcagent.tools._decorator import hook, tool

_logger = logging.getLogger("arcagent.modules.planning.capabilities")


# --- Wiring helpers --------------------------------------------------------


def _goal_drift_error(plan: Plan) -> str | None:
    """Refuse a plan whose identity-goal binding has drifted (ASI01).

    Returns a JSON error string when the plan's stored ``parent_goal_hash`` no
    longer matches the current identity.md goal charter — the plan was authored
    under goals the agent no longer holds, so executing it further would be goal
    hijack. Returns None when the binding still holds.
    """
    if plan.parent_goal_hash != _runtime.identity_goal_hash():
        return json.dumps(
            {"error": "plan refused: agent identity goals changed since creation (goal drift)"}
        )
    return None


def _adapt_run_fn(agent_run_fn: Any, plan_id: str, *, isolated: bool = False) -> Any:
    """Adapt ``agent.run_collected`` to the executor's ``RunFn`` seam.

    Sequentially, all steps of a plan share one session so later steps see
    earlier context. Under concurrent dispatch (``isolated=True``) each branch
    gets its OWN session so parallel branches cannot clobber one shared
    session's turn history mid-flight — the DAG frontier is independent by
    construction, so no cross-branch context is expected (SPEC-043 F1).

    The per-step budget slice is forwarded so the run's LLM10 breaker bounds
    the step, and the terminal outcome the run reports (``completion_payload``
    / ``completion_tool`` / observed tokens) is mapped into the ``LoopResult``
    so a policy DENY, tool error, or budget breach classifies as FAILED — not
    a falsified SUCCEEDED (SPEC-040 F1/F2, REQ-021/023).
    """

    async def run_fn(
        *, task: str, max_tokens: int | None, max_cost_usd: float | None, actor_did: str
    ) -> LoopResult:
        session_key = f"plan-{plan_id}-{uuid4().hex}" if isolated else f"plan-{plan_id}"
        rr = await agent_run_fn(
            task,
            session_key=session_key,
            max_tokens=max_tokens,
            max_cost_usd=max_cost_usd,
        )
        return LoopResult(
            content=rr.content or "",
            turns=rr.turns,
            tool_calls_made=rr.tool_calls_made,
            tokens_used=dict(rr.tokens_used),
            strategy_used="react",
            cost_usd=rr.cost_usd,
            events=[],
            completion_payload=rr.completion_payload,
            completion_tool=rr.completion_tool,
        )

    return run_fn


def _build_orchestrator(plan_id: str) -> PlanOrchestrator:
    """Assemble the orchestrator for one plan from the current runtime state."""
    st = _runtime.state()

    async def replan_fn(plan: Plan, reason: str) -> Plan:
        return await replan(
            plan, reason, model=_runtime.get_model(), known_tools=st.known_tools
        )

    executor: StepExecutor
    if st.config.concurrent:
        run_fn = _adapt_run_fn(st.run_fn, plan_id, isolated=True)
        executor = ConcurrentStepExecutor(
            run_fn,
            actor_did=st.agent_did,
            max_parallel=st.config.max_parallel,
        )
    else:
        run_fn = _adapt_run_fn(st.run_fn, plan_id)
        executor = ArcRunStepExecutor(run_fn, actor_did=st.agent_did)
    return PlanOrchestrator(st.store, executor, replan_fn=replan_fn)


def _plan_report(plan: Plan) -> dict[str, Any]:
    return {
        "plan_id": plan.plan_id,
        "goal": plan.goal,
        "status": plan.status.value,
        "version": plan.version,
        "replans_used": plan.replans_used,
        "steps": [
            {
                "step_id": s.step_id,
                "description": s.description,
                "depends_on": s.depends_on,
                "status": s.status.value,
                "result": s.result,
                "failure_reason": s.failure_reason,
            }
            for s in plan.steps
        ],
    }


# --- Tools -----------------------------------------------------------------


@tool(
    name="plan_create",
    description=(
        "Decompose a goal into a dependency-aware plan and execute it. The "
        "plan is durable and resumable; each step runs as a bounded, "
        "policy-gated sub-task. Returns the final plan status."
    ),
    classification="state_modifying",
    capability_tags=("planning",),
    when_to_use="When a goal needs multiple ordered steps to accomplish.",
)
async def plan_create(goal: str) -> str:
    """Decompose ``goal`` into a DAG, persist it, and drive it to a terminal state."""
    if not goal:
        return json.dumps({"error": "goal is required"})
    st = _runtime.state()
    if st.run_fn is None:
        return json.dumps({"error": "planner not ready: no agent run seam bound"})
    model = _runtime.get_model()
    if model is None:
        return json.dumps({"error": "planner has no model configured"})
    try:
        plan = await decompose(
            goal,
            model=model,
            goal_source_did=st.agent_did or "did:arc:user",
            parent_goal_hash=_runtime.identity_goal_hash(),
            budget=st.budget,
            max_replans=st.max_replans,
            known_tools=st.known_tools,
        )
    except DecompositionError as exc:
        return json.dumps({"error": f"decomposition rejected: {exc}"})
    st.store.save(plan, action="plan.created")
    orchestrator = _build_orchestrator(plan.plan_id)
    final = await orchestrator.execute(plan)
    return json.dumps(_plan_report(final))


@tool(
    name="plan_status",
    description="Report the active plan and the status of each of its steps.",
    classification="read_only",
    capability_tags=("planning",),
    when_to_use="When you want to review plan progress.",
)
async def plan_status() -> str:
    """Return the active plan report, or a note that none is active."""
    plan = _runtime.state().store.active_plan()
    if plan is None:
        return json.dumps({"active_plan": None})
    return json.dumps(_plan_report(plan))


@tool(
    name="plan_replan",
    description="Force a bounded revision of the remaining plan given a reason.",
    classification="state_modifying",
    capability_tags=("planning",),
    when_to_use="When the current plan no longer fits reality and needs revising.",
)
async def plan_replan(reason: str) -> str:
    """Replan the remainder of the active plan and continue executing it."""
    st = _runtime.state()
    plan = st.store.active_plan()
    if plan is None:
        return json.dumps({"error": "no active plan to replan"})
    drift = _goal_drift_error(plan)
    if drift is not None:
        return drift
    if plan.replans_used >= plan.max_replans:
        return json.dumps({"error": "replan budget exhausted"})
    orchestrator = _build_orchestrator(plan.plan_id)
    revised = await replan(
        plan, reason or "manual replan", model=_runtime.get_model(), known_tools=st.known_tools
    )
    st.store.save(
        revised,
        action="plan.replanned",
        extra={"trigger": reason, "from_version": plan.version, "to_version": revised.version},
    )
    final = await orchestrator.execute(revised)
    return json.dumps(_plan_report(final))


@tool(
    name="plan_abandon",
    description="Abandon the active plan (records the reason).",
    classification="state_modifying",
    capability_tags=("planning",),
    when_to_use="When the goal is no longer valid and the plan should stop.",
)
async def plan_abandon(reason: str) -> str:
    """Mark the active plan ABANDONED and checkpoint it."""
    st = _runtime.state()
    plan = st.store.active_plan()
    if plan is None:
        return json.dumps({"error": "no active plan to abandon"})
    plan.status = PlanStatus.ABANDONED
    st.store.save(plan, action="plan.abandoned", outcome="abandoned", extra={"reason": reason})
    return json.dumps({"plan_id": plan.plan_id, "status": plan.status.value})


# --- Hooks -----------------------------------------------------------------


@hook(event="agent:assemble_prompt", priority=60)
async def inject_planning_section(ctx: Any) -> None:
    """Inject the active plan frontier into the system prompt (REQ-041)."""
    sections = ctx.data.get("sections")
    if sections is None or not isinstance(sections, dict):
        return
    try:
        plan = _runtime.state().store.active_plan()
    except RuntimeError:
        return
    if plan is None:
        return
    ready = {s.step_id for s in plan.ready_steps()}
    lines = [f"## Active Plan: {plan.goal} (v{plan.version}, {plan.status.value})", ""]
    for step in plan.steps:
        marker = " <- ready" if step.step_id in ready else ""
        lines.append(f"- **[{step.status.value}]** `{step.step_id}`: {step.description}{marker}")
    sections["planning"] = "\n".join(lines)


@hook(event="agent:ready", priority=100)
async def planning_bind_run_fn(ctx: Any) -> None:
    """Bind the agent run seam so steps drive real, gated arcrun runs."""
    data = ctx.data if hasattr(ctx, "data") else {}
    run_fn = data.get("run_fn")
    if run_fn is not None:
        _runtime.state().run_fn = run_fn
        _logger.info("Planning bound agent run seam for step execution")


@hook(event="agent:shutdown", priority=100)
async def planning_shutdown(ctx: Any) -> None:
    """Plans are checkpointed on every transition; nothing to flush."""
    del ctx
    _logger.info("Planning module stopped")


__all__ = [
    "inject_planning_section",
    "plan_abandon",
    "plan_create",
    "plan_replan",
    "plan_status",
    "planning_bind_run_fn",
    "planning_shutdown",
]
