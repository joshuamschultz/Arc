"""Plan data model — a DAG of dependency-aware steps (SPEC-040).

The planner owns this artifact; arcrun executes a single step, arcllm
produces/revises the DAG, existing infra persists + audits it. The model
is deliberately dumb data with three pure derivations:

* :meth:`Plan.validate_dag` — reject cycles / dangling / duplicate edges.
* :meth:`Plan.ready_steps` — re-derive the execution frontier from
  ``depends_on`` + the ``SUCCEEDED`` set (no separate cursor, so a resume
  reconstructs progress from the file alone).
* :meth:`Plan.is_terminal` / :meth:`Plan.terminal_status` — decide when the
  walk stops and whether it completed or failed.

The DAG representation is LLMCompiler-style (``depends_on`` edges); SPEC-043
will dispatch independent branches in parallel behind the ``StepExecutor``
seam without changing this model.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class StepStatus(StrEnum):
    """Lifecycle of a single plan step (typed, never free-text)."""

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class PlanStatus(StrEnum):
    """Lifecycle of a whole plan."""

    DRAFT = "draft"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


class PlanBudget(BaseModel):
    """Aggregate ceiling for a plan, sliced onto per-step run ceilings.

    Maps onto arcrun's SPEC-038 ``max_tokens`` / ``max_cost_usd`` so a plan
    can never exceed the run's budget (REQ-022). Frozen: a plan's budget is
    fixed at creation and only the operator config, never the agent, sets it.
    """

    model_config = ConfigDict(frozen=True)

    max_tokens: int | None = None
    max_cost_usd: float | None = None


class PlanStep(BaseModel):
    """One node in the plan DAG.

    Mutable: the orchestrator advances ``status`` and records ``result`` /
    ``failure_reason`` in place, then checkpoints the whole plan.
    """

    step_id: str
    description: str
    depends_on: list[str] = Field(default_factory=list)
    # Advisory only (REQ-004) — dispatch authority stays with the policy
    # pipeline; a hint never grants a tool.
    tool_hint: str | None = None
    status: StepStatus = StepStatus.PENDING
    # Observation captured on success (fed to a later replan).
    result: str | None = None
    # DENY / budget breach / tool error captured on failure (REQ-023).
    failure_reason: str | None = None
    attempts: int = 0


class Plan(BaseModel):
    """A durable, dependency-aware plan for one goal.

    Binds to the agent's immutable identity goals via ``parent_goal_hash``
    (ASI01) and records provenance via ``goal_source_did`` (REQ-003).
    """

    plan_id: str
    goal: str
    goal_source_did: str
    parent_goal_hash: str
    status: PlanStatus = PlanStatus.DRAFT
    version: int = 1
    steps: list[PlanStep] = Field(default_factory=list)
    max_replans: int = 3
    replans_used: int = 0
    budget: PlanBudget = Field(default_factory=PlanBudget)
    # Cumulative consumption across every step run, checkpointed with the plan
    # so the aggregate ceiling survives a resume (REQ-022, LLM10).
    tokens_spent: int = 0
    cost_spent: float = 0.0
    # SPEC-043 REQ-053 — outstanding reservations for in-flight concurrent
    # branches. Reserve-then-settle: a branch reserves its cap from the shared
    # budget BEFORE launch and settles actual spend on completion, so
    # ``Σ(reservations + spend) ≤ budget`` and N concurrent branches can never
    # overspend the aggregate. Zero when no branch is in flight (sequential).
    reserved_tokens: int = 0
    reserved_cost: float = 0.0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # --- Lookups ----------------------------------------------------------

    def get_step(self, step_id: str) -> PlanStep:
        """Return the step with ``step_id`` or raise ``KeyError``."""
        for step in self.steps:
            if step.step_id == step_id:
                return step
        raise KeyError(step_id)

    # --- Structural validation (REQ-001) ----------------------------------

    def validate_dag(self) -> None:
        """Reject duplicate ids, dangling edges, and cycles.

        A plan that does not describe a valid DAG is never persisted or
        executed — an ungrounded structure is a poisoned plan (ASI06).
        """
        ids = [s.step_id for s in self.steps]
        known = set(ids)
        if len(known) != len(ids):
            raise ValueError("plan has duplicate step ids")
        for step in self.steps:
            for dep in step.depends_on:
                if dep not in known:
                    raise ValueError(f"step {step.step_id!r} depends on unknown step {dep!r}")
        self._reject_cycles()

    def _reject_cycles(self) -> None:
        """Kahn topological sort; any leftover node means a cycle."""
        indegree = {s.step_id: len(s.depends_on) for s in self.steps}
        dependents: dict[str, list[str]] = {s.step_id: [] for s in self.steps}
        for step in self.steps:
            for dep in step.depends_on:
                dependents[dep].append(step.step_id)
        queue = [sid for sid, deg in indegree.items() if deg == 0]
        visited = 0
        while queue:
            node = queue.pop()
            visited += 1
            for child in dependents[node]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        if visited != len(self.steps):
            raise ValueError("plan DAG contains a cycle")

    # --- Frontier + termination (REQ-012, REQ-024, REQ-030) ---------------

    def ready_steps(self) -> list[PlanStep]:
        """Return pending steps whose dependencies have all succeeded.

        Re-derived every call from ``depends_on`` + the ``SUCCEEDED`` set,
        so a resume reconstructs the frontier from the plan file alone.
        Order follows step declaration (a valid topological order for a
        well-formed DAG).
        """
        succeeded = {s.step_id for s in self.steps if s.status is StepStatus.SUCCEEDED}
        return [
            step
            for step in self.steps
            if step.status is StepStatus.PENDING
            and all(dep in succeeded for dep in step.depends_on)
        ]

    def is_complete(self) -> bool:
        """True when every step succeeded or was skipped."""
        return all(s.status in (StepStatus.SUCCEEDED, StepStatus.SKIPPED) for s in self.steps)

    def has_running(self) -> bool:
        """True when any step is mid-flight."""
        return any(s.status is StepStatus.RUNNING for s in self.steps)

    def is_terminal(self) -> bool:
        """True when there is nothing left the walk can advance.

        Either the plan is complete, or the frontier is empty and nothing
        is running (a failed step has blocked the remainder).
        """
        if self.is_complete():
            return True
        return not self.ready_steps() and not self.has_running()

    def terminal_status(self) -> PlanStatus:
        """COMPLETED when fully succeeded, else FAILED."""
        return PlanStatus.COMPLETED if self.is_complete() else PlanStatus.FAILED

    def budget_exhausted(self) -> bool:
        """True once cumulative consumption reaches the aggregate ceiling.

        The plan-level LLM10 stop: enforced across steps (not per-step only)
        so N steps can never sum past the plan budget (REQ-022). ``None`` on a
        dimension means that dimension is unbounded.
        """
        b = self.budget
        if b.max_tokens is not None and self.tokens_spent >= b.max_tokens:
            return True
        return b.max_cost_usd is not None and self.cost_spent >= b.max_cost_usd

    def remaining_budget(self) -> tuple[int | None, float | None]:
        """Aggregate budget left, floored at zero; ``None`` stays unbounded."""
        b = self.budget
        return (
            None if b.max_tokens is None else max(0, b.max_tokens - self.tokens_spent),
            None if b.max_cost_usd is None else max(0.0, b.max_cost_usd - self.cost_spent),
        )

    def available_budget(self) -> tuple[int | None, float | None]:
        """Budget available to RESERVE now: remaining minus outstanding reservations.

        The admission signal for concurrent branch dispatch (REQ-053): each
        in-flight branch's reservation is already subtracted, so a later branch
        sees less headroom and the (N+1)-th branch that would breach gets zero.
        Floored at zero; ``None`` on a dimension stays unbounded.
        """
        b = self.budget
        return (
            None
            if b.max_tokens is None
            else max(0, b.max_tokens - self.tokens_spent - self.reserved_tokens),
            None
            if b.max_cost_usd is None
            else max(0.0, b.max_cost_usd - self.cost_spent - self.reserved_cost),
        )

    def touch(self) -> None:
        """Stamp ``updated_at`` — called before every checkpoint."""
        self.updated_at = datetime.now(UTC)


__all__ = [
    "Plan",
    "PlanBudget",
    "PlanStatus",
    "PlanStep",
    "StepStatus",
]
