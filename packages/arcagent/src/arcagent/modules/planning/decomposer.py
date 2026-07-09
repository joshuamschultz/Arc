"""Goal -> Plan decomposition and remaining-plan revision, via arcllm.

Inference is arcllm's job: the planner asks the model for a structured DAG
through the **portable tool-forced path** (a single forced tool call whose
arguments validate into a plan draft). The module imports no provider adapter
and runs no turn-loop — that would cross the concern boundary (REQ-002).

Decomposition is ReWOO-style: one upfront planner call produces the whole DAG
(REQ-001). Replan is Reflexion-lite: on a failed step the model is re-invoked
with the goal, the completed results, and the failure reason to revise only the
remainder — the succeeded prefix is never discarded (REQ-030).

Every returned plan is grounded (REQ-005) and refused if it targets a protected
identity path (REQ-040) — an ungrounded or goal-hijacking plan is raised, never
persisted.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from typing import Any, Protocol

from arcllm import Message, Tool
from pydantic import BaseModel, Field, ValidationError

from arcagent.modules.planning.models import (
    Plan,
    PlanBudget,
    PlanStatus,
    PlanStep,
    StepStatus,
)

# Identity artifacts a plan may never target (ASI01). The runtime protected-path
# denylist (SPEC-035) enforces this at write time; we reject earlier, at plan
# construction, so an ungrounded plan is never even persisted.
_PROTECTED_NAMES = ("identity.md", "policy.md")

_TOOL_NAME = "emit_plan"


class DecompositionError(ValueError):
    """Raised when the model returns an ungrounded, malformed, or hijacking plan."""


class PlanModel(Protocol):
    """Minimal arcllm surface the decomposer needs (one structured call)."""

    async def invoke(self, messages: Any, tools: Any = None, **kwargs: Any) -> Any: ...


class _StepDraft(BaseModel):
    step_id: str
    description: str
    depends_on: list[str] = Field(default_factory=list)
    tool_hint: str | None = None


class _PlanDraft(BaseModel):
    steps: list[_StepDraft]


def _plan_tool() -> Tool:
    return Tool(
        name=_TOOL_NAME,
        description=(
            "Emit a plan as a DAG of steps. Each step has a unique step_id, a "
            "description, a depends_on list of earlier step_ids, and an optional "
            "advisory tool_hint. Order steps so dependencies come first."
        ),
        parameters=_PlanDraft.model_json_schema(),
    )


async def _invoke_for_draft(model: PlanModel, messages: list[Message]) -> _PlanDraft:
    """One forced structured call; validate the arguments into a plan draft."""
    response = await model.invoke(
        messages,
        tools=[_plan_tool()],
        tool_choice={"type": "tool", "name": _TOOL_NAME},
    )
    calls = getattr(response, "tool_calls", None) or []
    if not calls:
        raise DecompositionError("model returned no structured plan")
    try:
        return _PlanDraft.model_validate(calls[0].arguments)
    except ValidationError as exc:
        raise DecompositionError(f"model plan failed schema validation: {exc}") from exc


def _targets_protected_path(step: PlanStep) -> bool:
    haystack = f"{step.description} {step.tool_hint or ''}".lower()
    return any(name in haystack for name in _PROTECTED_NAMES)


def _ground(steps: Sequence[PlanStep], known_tools: Iterable[str]) -> None:
    """Reject ungrounded or goal-hijacking plans (REQ-005/040)."""
    if not steps:
        raise DecompositionError("plan has no steps")
    for step in steps:
        if _targets_protected_path(step):
            raise DecompositionError(
                f"step {step.step_id!r} targets a protected identity path — refused"
            )
    known = set(known_tools)
    hints = [s.tool_hint for s in steps if s.tool_hint]
    if known and hints and not any(hint in known for hint in hints):
        raise DecompositionError(
            "plan references no known capability — cannot be grounded"
        )


def _steps_from_draft(draft: _PlanDraft) -> list[PlanStep]:
    return [
        PlanStep(
            step_id=d.step_id,
            description=d.description,
            depends_on=list(d.depends_on),
            tool_hint=d.tool_hint,
        )
        for d in draft.steps
    ]


def _validate_or_raise(plan: Plan) -> None:
    try:
        plan.validate_dag()
    except ValueError as exc:
        raise DecompositionError(str(exc)) from exc


_SYSTEM = (
    "You are a planner. Decompose the user's goal into the smallest correct DAG "
    "of concrete steps. Use depends_on to order dependent work. Never target "
    "identity.md or policy.md. Emit the plan via the emit_plan tool."
)


async def decompose(
    goal: str,
    *,
    model: PlanModel,
    goal_source_did: str,
    parent_goal_hash: str,
    budget: PlanBudget,
    max_replans: int,
    known_tools: Iterable[str],
    plan_id: str | None = None,
) -> Plan:
    """Goal -> validated, grounded :class:`Plan` (ACTIVE) — never persisted here."""
    messages = [
        Message(role="system", content=_SYSTEM),
        Message(role="user", content=f"Goal: {goal}"),
    ]
    draft = await _invoke_for_draft(model, messages)
    steps = _steps_from_draft(draft)
    _ground(steps, known_tools)
    plan = Plan(
        plan_id=plan_id or f"plan_{uuid.uuid4().hex[:12]}",
        goal=goal,
        goal_source_did=goal_source_did,
        parent_goal_hash=parent_goal_hash,
        status=PlanStatus.ACTIVE,
        steps=steps,
        max_replans=max_replans,
        budget=budget,
    )
    _validate_or_raise(plan)
    return plan


def _completed_summary(plan: Plan) -> str:
    done = [s for s in plan.steps if s.status is StepStatus.SUCCEEDED]
    if not done:
        return "(no steps completed yet)"
    return "\n".join(f"- {s.step_id}: {s.result or 'ok'}" for s in done)


async def replan(
    plan: Plan,
    failure_reason: str,
    *,
    model: PlanModel,
    known_tools: Iterable[str],
) -> Plan:
    """Revise the *remaining* plan around a failure (REQ-030/032).

    Preserves the SUCCEEDED prefix (with results), feeds the model the real
    completed results + failure reason, replaces the remainder, bumps
    ``version`` and ``replans_used``. The caller persists + audits the result.
    """
    succeeded = [s for s in plan.steps if s.status is StepStatus.SUCCEEDED]
    messages = [
        Message(role="system", content=_SYSTEM),
        Message(
            role="user",
            content=(
                f"Goal: {plan.goal}\n\n"
                f"Completed steps and results:\n{_completed_summary(plan)}\n\n"
                f"The next step failed with reason: {failure_reason}\n\n"
                "Emit a revised plan for the REMAINING work only. Do not repeat "
                "completed steps. New steps may depend on completed step ids."
            ),
        ),
    ]
    draft = await _invoke_for_draft(model, messages)
    new_steps = _steps_from_draft(draft)
    _ground(new_steps, known_tools)
    revised = Plan(
        plan_id=plan.plan_id,
        goal=plan.goal,
        goal_source_did=plan.goal_source_did,
        parent_goal_hash=plan.parent_goal_hash,
        status=PlanStatus.ACTIVE,
        version=plan.version + 1,
        steps=succeeded + new_steps,
        max_replans=plan.max_replans,
        replans_used=plan.replans_used + 1,
        budget=plan.budget,
        created_at=plan.created_at,
    )
    _validate_or_raise(revised)
    return revised


__all__ = [
    "DecompositionError",
    "PlanModel",
    "decompose",
    "replan",
]
