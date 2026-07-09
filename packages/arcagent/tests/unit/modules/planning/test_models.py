"""Unit tests for the Plan/PlanStep data model (SPEC-040 T-010..T-013)."""

from __future__ import annotations

import pytest

from arcagent.modules.planning.models import (
    Plan,
    PlanBudget,
    PlanStatus,
    PlanStep,
    StepStatus,
)


def _step(step_id: str, deps: list[str] | None = None) -> PlanStep:
    return PlanStep(step_id=step_id, description=f"do {step_id}", depends_on=deps or [])


def _plan(steps: list[PlanStep]) -> Plan:
    return Plan(
        plan_id="plan_1",
        goal="reach the goal",
        goal_source_did="did:arc:user",
        parent_goal_hash="deadbeef",
        status=PlanStatus.ACTIVE,
        steps=steps,
        max_replans=3,
        budget=PlanBudget(max_tokens=1000, max_cost_usd=1.0),
    )


class TestModelRoundTrip:
    def test_serialize_deserialize_equal(self) -> None:
        plan = _plan([_step("a"), _step("b", ["a"])])
        raw = plan.model_dump_json()
        restored = Plan.model_validate_json(raw)
        assert restored == plan

    def test_step_defaults(self) -> None:
        step = PlanStep(step_id="a", description="x")
        assert step.status is StepStatus.PENDING
        assert step.depends_on == []
        assert step.result is None
        assert step.failure_reason is None
        assert step.attempts == 0
        assert step.tool_hint is None

    def test_plan_defaults(self) -> None:
        plan = _plan([_step("a")])
        assert plan.version == 1
        assert plan.replans_used == 0


class TestValidateDag:
    def test_valid_dag_passes(self) -> None:
        plan = _plan([_step("a"), _step("b", ["a"]), _step("c", ["a", "b"])])
        plan.validate_dag()  # no raise

    def test_cycle_raises(self) -> None:
        plan = _plan([_step("a", ["b"]), _step("b", ["a"])])
        with pytest.raises(ValueError, match="cycle"):
            plan.validate_dag()

    def test_dangling_dependency_raises(self) -> None:
        plan = _plan([_step("a", ["ghost"])])
        with pytest.raises(ValueError, match="unknown|dangling"):
            plan.validate_dag()

    def test_duplicate_step_id_raises(self) -> None:
        plan = _plan([_step("a"), _step("a")])
        with pytest.raises(ValueError, match="duplicate"):
            plan.validate_dag()


class TestReadySteps:
    def test_initial_frontier_is_dependency_free(self) -> None:
        plan = _plan([_step("a"), _step("b", ["a"]), _step("c")])
        ready = {s.step_id for s in plan.ready_steps()}
        assert ready == {"a", "c"}

    def test_frontier_advances_as_deps_succeed(self) -> None:
        plan = _plan([_step("a"), _step("b", ["a"]), _step("c", ["a", "b"])])
        plan.get_step("a").status = StepStatus.SUCCEEDED
        assert {s.step_id for s in plan.ready_steps()} == {"b"}
        plan.get_step("b").status = StepStatus.SUCCEEDED
        assert {s.step_id for s in plan.ready_steps()} == {"c"}

    def test_diamond_frontier(self) -> None:
        # a -> {b, c} -> d
        plan = _plan(
            [_step("a"), _step("b", ["a"]), _step("c", ["a"]), _step("d", ["b", "c"])]
        )
        plan.get_step("a").status = StepStatus.SUCCEEDED
        assert {s.step_id for s in plan.ready_steps()} == {"b", "c"}

    def test_running_step_not_ready(self) -> None:
        plan = _plan([_step("a")])
        plan.get_step("a").status = StepStatus.RUNNING
        assert plan.ready_steps() == []

    def test_failed_dependency_blocks_frontier(self) -> None:
        plan = _plan([_step("a"), _step("b", ["a"])])
        plan.get_step("a").status = StepStatus.FAILED
        assert plan.ready_steps() == []


class TestTerminal:
    def test_all_succeeded_is_complete_and_terminal(self) -> None:
        plan = _plan([_step("a"), _step("b", ["a"])])
        for s in plan.steps:
            s.status = StepStatus.SUCCEEDED
        assert plan.is_complete()
        assert plan.is_terminal()
        assert plan.terminal_status() is PlanStatus.COMPLETED

    def test_skipped_counts_as_complete(self) -> None:
        plan = _plan([_step("a")])
        plan.get_step("a").status = StepStatus.SKIPPED
        assert plan.is_complete()
        assert plan.terminal_status() is PlanStatus.COMPLETED

    def test_blocked_failure_is_terminal_failed(self) -> None:
        plan = _plan([_step("a"), _step("b", ["a"])])
        plan.get_step("a").status = StepStatus.FAILED
        assert not plan.is_complete()
        assert plan.is_terminal()
        assert plan.terminal_status() is PlanStatus.FAILED

    def test_in_progress_not_terminal(self) -> None:
        plan = _plan([_step("a"), _step("b", ["a"])])
        assert not plan.is_terminal()
