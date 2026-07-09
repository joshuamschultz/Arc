"""Unit tests for the Plan-Execute orchestrator (SPEC-040 T-050..T-052)."""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust import AuditEvent

from arcagent.modules.planning.executor import StepOutcome
from arcagent.modules.planning.models import (
    Plan,
    PlanBudget,
    PlanStatus,
    PlanStep,
    StepStatus,
)
from arcagent.modules.planning.orchestrator import PlanOrchestrator
from arcagent.modules.planning.store import PlanStore


class _CapturingSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _ScriptedExecutor:
    """Returns a scripted outcome per step_id and records the call order."""

    def __init__(self, outcomes: dict[str, StepOutcome]) -> None:
        self._outcomes = outcomes
        self.ran: list[str] = []

    async def run_step(self, step: PlanStep, *, plan: Plan) -> StepOutcome:
        self.ran.append(step.step_id)
        return self._outcomes.get(step.step_id, StepOutcome(StepStatus.SUCCEEDED, result="ok"))


def _plan(steps: list[PlanStep], *, max_replans: int = 2) -> Plan:
    return Plan(
        plan_id="plan_1",
        goal="g",
        goal_source_did="did:arc:user",
        parent_goal_hash="h",
        status=PlanStatus.ACTIVE,
        steps=steps,
        max_replans=max_replans,
        budget=PlanBudget(max_tokens=900),
    )


def _store(tmp_path: Path, sink: _CapturingSink) -> PlanStore:
    return PlanStore(tmp_path / "plans", audit_sink=sink, actor_did="did:arc:agent")


async def _never_replan(plan: Plan, reason: str) -> Plan:  # pragma: no cover
    raise AssertionError("replan should not be called")


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_linear_plan_completes_in_dependency_order(self, tmp_path: Path) -> None:
        sink = _CapturingSink()
        store = _store(tmp_path, sink)
        plan = _plan(
            [
                PlanStep(step_id="a", description="a"),
                PlanStep(step_id="b", description="b", depends_on=["a"]),
                PlanStep(step_id="c", description="c", depends_on=["b"]),
            ]
        )
        store.save(plan, action="plan.created")
        ex = _ScriptedExecutor({})
        orch = PlanOrchestrator(store, ex, replan_fn=_never_replan)
        final = await orch.execute(plan)
        assert final.status is PlanStatus.COMPLETED
        assert ex.ran == ["a", "b", "c"]  # topological order

    @pytest.mark.asyncio
    async def test_checkpoint_after_every_transition(self, tmp_path: Path) -> None:
        sink = _CapturingSink()
        store = _store(tmp_path, sink)
        plan = _plan([PlanStep(step_id="a", description="a")])
        store.save(plan, action="plan.created")
        orch = PlanOrchestrator(store, _ScriptedExecutor({}), replan_fn=_never_replan)
        await orch.execute(plan)
        actions = [e.action for e in sink.events]
        assert actions == [
            "plan.created",
            "plan.step.started",
            "plan.step.succeeded",
            "plan.completed",
        ]
        # Plan file on disk is the durable resume record.
        assert (tmp_path / "plans" / "plan_1.json").exists()


class TestReplan:
    @pytest.mark.asyncio
    async def test_failed_step_triggers_bounded_replan_then_completes(
        self, tmp_path: Path
    ) -> None:
        sink = _CapturingSink()
        store = _store(tmp_path, sink)
        plan = _plan([PlanStep(step_id="a", description="a")])
        store.save(plan, action="plan.created")
        ex = _ScriptedExecutor({"a": StepOutcome(StepStatus.FAILED, failure_reason="denied")})

        async def replan_fn(p: Plan, reason: str) -> Plan:
            assert reason == "denied"
            return Plan(
                plan_id=p.plan_id,
                goal=p.goal,
                goal_source_did=p.goal_source_did,
                parent_goal_hash=p.parent_goal_hash,
                status=PlanStatus.ACTIVE,
                version=p.version + 1,
                steps=[PlanStep(step_id="a2", description="a retried")],
                max_replans=p.max_replans,
                replans_used=p.replans_used + 1,
                budget=p.budget,
            )

        orch = PlanOrchestrator(store, ex, replan_fn=replan_fn)
        final = await orch.execute(plan)
        assert final.status is PlanStatus.COMPLETED
        assert "plan.replanned" in [e.action for e in sink.events]
        assert ex.ran == ["a", "a2"]

    @pytest.mark.asyncio
    async def test_max_replans_exhaustion_terminates_failed(self, tmp_path: Path) -> None:
        sink = _CapturingSink()
        store = _store(tmp_path, sink)
        plan = _plan([PlanStep(step_id="s0", description="s0")], max_replans=2)
        store.save(plan, action="plan.created")

        # Executor always fails whatever step it is handed.
        class _AlwaysFail:
            def __init__(self) -> None:
                self.count = 0

            async def run_step(self, step: PlanStep, *, plan: Plan) -> StepOutcome:
                self.count += 1
                return StepOutcome(StepStatus.FAILED, failure_reason="nope")

        replans: list[int] = []

        async def replan_fn(p: Plan, reason: str) -> Plan:
            replans.append(1)
            return Plan(
                plan_id=p.plan_id,
                goal=p.goal,
                goal_source_did=p.goal_source_did,
                parent_goal_hash=p.parent_goal_hash,
                status=PlanStatus.ACTIVE,
                version=p.version + 1,
                steps=[PlanStep(step_id=f"s{p.version}", description="retry")],
                max_replans=p.max_replans,
                replans_used=p.replans_used + 1,
                budget=p.budget,
            )

        ex = _AlwaysFail()
        orch = PlanOrchestrator(store, ex, replan_fn=replan_fn)
        final = await orch.execute(plan)
        assert final.status is PlanStatus.FAILED
        assert sum(replans) == 2  # exactly max_replans revisions, then stop
        # The terminal event carries the structured terminator.
        failed = [e for e in sink.events if e.action == "plan.failed"][-1]
        assert failed.extra["terminator"]["error"] == "max_replans"


class TestAggregateBudget:
    @pytest.mark.asyncio
    async def test_runaway_plan_stops_at_aggregate_ceiling(self, tmp_path: Path) -> None:
        """Cumulative step spend halts a multi-step plan — not per-step only."""
        sink = _CapturingSink()
        store = _store(tmp_path, sink)
        # Budget is 900 tokens; each step burns 500 → step a (500) then step b
        # would cross 900, so the plan finalizes FAILED before running c.
        plan = _plan(
            [
                PlanStep(step_id="a", description="a"),
                PlanStep(step_id="b", description="b", depends_on=["a"]),
                PlanStep(step_id="c", description="c", depends_on=["b"]),
            ],
            max_replans=0,
        )
        store.save(plan, action="plan.created")

        class _Burner:
            def __init__(self) -> None:
                self.ran: list[str] = []

            async def run_step(self, step: PlanStep, *, plan: Plan) -> StepOutcome:
                self.ran.append(step.step_id)
                return StepOutcome(StepStatus.SUCCEEDED, result="ok", tokens_used=500)

        ex = _Burner()
        orch = PlanOrchestrator(store, ex, replan_fn=_never_replan)
        final = await orch.execute(plan)
        assert final.status is PlanStatus.FAILED  # aggregate ceiling stopped it
        assert ex.ran == ["a", "b"]  # c never dispatched — total bounded
        assert final.tokens_spent == 1000  # a(500) + b(500), checkpointed
        assert final.budget_exhausted()


class TestResume:
    @pytest.mark.asyncio
    async def test_resume_skips_succeeded_steps(self, tmp_path: Path) -> None:
        sink = _CapturingSink()
        store = _store(tmp_path, sink)
        plan = _plan(
            [
                PlanStep(step_id="a", description="a", status=StepStatus.SUCCEEDED, result="done"),
                PlanStep(step_id="b", description="b", depends_on=["a"]),
            ]
        )
        store.save(plan, action="plan.created")

        # A fresh orchestrator + executor: simulates a restart.
        ex = _ScriptedExecutor({})
        orch = PlanOrchestrator(store, ex, replan_fn=_never_replan)
        final = await orch.resume()
        assert final is not None
        assert final.status is PlanStatus.COMPLETED
        assert ex.ran == ["b"]  # 'a' already succeeded — not re-run

    @pytest.mark.asyncio
    async def test_resume_returns_none_without_active_plan(self, tmp_path: Path) -> None:
        sink = _CapturingSink()
        store = _store(tmp_path, sink)
        orch = PlanOrchestrator(store, _ScriptedExecutor({}), replan_fn=_never_replan)
        assert await orch.resume() is None
