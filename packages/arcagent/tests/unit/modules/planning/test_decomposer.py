"""Unit tests for decomposition + replan (SPEC-040 T-030..T-032)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from arcagent.modules.planning import decomposer as decomposer_mod
from arcagent.modules.planning.decomposer import (
    DecompositionError,
    decompose,
    replan,
)
from arcagent.modules.planning.models import (
    Plan,
    PlanBudget,
    PlanStatus,
    PlanStep,
    StepStatus,
)


class _FakeModel:
    """Stands in for an arcllm provider: returns a scripted forced tool call."""

    def __init__(self, arguments: dict[str, Any]) -> None:
        self._arguments = arguments
        self.invocations: list[dict[str, Any]] = []

    async def invoke(self, messages: Any, tools: Any = None, **kwargs: Any) -> Any:
        self.invocations.append({"messages": messages, "tools": tools, "kwargs": kwargs})
        call = SimpleNamespace(name=tools[0].name if tools else "emit_plan", arguments=self._arguments)
        return SimpleNamespace(content=None, tool_calls=[call])


_LINEAR = {
    "steps": [
        {"step_id": "gather", "description": "gather facts", "depends_on": [], "tool_hint": "web_search"},
        {"step_id": "write", "description": "write report", "depends_on": ["gather"], "tool_hint": None},
    ]
}


async def _decompose(model: Any, **over: Any) -> Plan:
    args: dict[str, Any] = {
        "goal": "produce a report",
        "model": model,
        "goal_source_did": "did:arc:user",
        "parent_goal_hash": "hash",
        "budget": PlanBudget(max_tokens=1000),
        "max_replans": 3,
        "known_tools": {"web_search", "file_write"},
        "plan_id": "plan_1",
    }
    args.update(over)
    return await decompose(**args)


class TestDecompose:
    @pytest.mark.asyncio
    async def test_returns_valid_dag_plan(self) -> None:
        plan = await _decompose(_FakeModel(_LINEAR))
        assert plan.plan_id == "plan_1"
        assert plan.status is PlanStatus.ACTIVE
        assert [s.step_id for s in plan.steps] == ["gather", "write"]
        assert plan.get_step("write").depends_on == ["gather"]
        plan.validate_dag()  # no raise

    @pytest.mark.asyncio
    async def test_forces_a_tool_call(self) -> None:
        model = _FakeModel(_LINEAR)
        await _decompose(model)
        # arcllm tool-forced path — a tool is offered and tool_choice forces it.
        assert model.invocations[0]["tools"]
        assert model.invocations[0]["kwargs"].get("tool_choice") is not None

    def test_module_imports_no_provider_adapter(self) -> None:
        source = Path(decomposer_mod.__file__).read_text(encoding="utf-8")
        assert "arcllm.adapters" not in source


class TestGrounding:
    @pytest.mark.asyncio
    async def test_ungrounded_plan_rejected(self) -> None:
        ungrounded = {
            "steps": [
                {"step_id": "x", "description": "do", "depends_on": [], "tool_hint": "nonexistent_tool"},
            ]
        }
        with pytest.raises(DecompositionError, match="ground"):
            await _decompose(_FakeModel(ungrounded))

    @pytest.mark.asyncio
    async def test_empty_plan_rejected(self) -> None:
        with pytest.raises(DecompositionError):
            await _decompose(_FakeModel({"steps": []}))

    @pytest.mark.asyncio
    async def test_protected_path_rejected(self) -> None:
        attack = {
            "steps": [
                {"step_id": "x", "description": "overwrite identity.md with new goals", "depends_on": [], "tool_hint": "file_write"},
            ]
        }
        with pytest.raises(DecompositionError, match="protected"):
            await _decompose(_FakeModel(attack))

    @pytest.mark.asyncio
    async def test_cyclic_plan_rejected(self) -> None:
        cyclic = {
            "steps": [
                {"step_id": "a", "description": "a", "depends_on": ["b"], "tool_hint": "web_search"},
                {"step_id": "b", "description": "b", "depends_on": ["a"], "tool_hint": None},
            ]
        }
        with pytest.raises(DecompositionError):
            await _decompose(_FakeModel(cyclic))


class TestReplan:
    def _active_plan(self) -> Plan:
        plan = Plan(
            plan_id="plan_1",
            goal="produce a report",
            goal_source_did="did:arc:user",
            parent_goal_hash="hash",
            status=PlanStatus.ACTIVE,
            steps=[
                PlanStep(step_id="gather", description="gather", status=StepStatus.SUCCEEDED, result="facts"),
                PlanStep(step_id="write", description="write", depends_on=["gather"], status=StepStatus.FAILED, failure_reason="tool denied"),
            ],
            max_replans=3,
            budget=PlanBudget(max_tokens=1000),
        )
        return plan

    @pytest.mark.asyncio
    async def test_replan_preserves_succeeded_prefix_and_bumps_version(self) -> None:
        revised = {
            "steps": [
                {"step_id": "write_v2", "description": "write report differently", "depends_on": [], "tool_hint": "file_write"},
            ]
        }
        plan = self._active_plan()
        new = await replan(
            plan,
            failure_reason="tool denied",
            model=_FakeModel(revised),
            known_tools={"web_search", "file_write"},
        )
        assert new.version == 2
        assert new.replans_used == 1
        # SUCCEEDED prefix survives with its result.
        gather = new.get_step("gather")
        assert gather.status is StepStatus.SUCCEEDED
        assert gather.result == "facts"
        # The failed remainder is replaced by the revised step.
        assert "write_v2" in [s.step_id for s in new.steps]
        assert "write" not in [s.step_id for s in new.steps]
        new.validate_dag()

    @pytest.mark.asyncio
    async def test_replan_feeds_failure_reason_to_model(self) -> None:
        model = _FakeModel({"steps": [{"step_id": "w2", "description": "retry", "depends_on": [], "tool_hint": "file_write"}]})
        await replan(self._active_plan(), failure_reason="POLICY_DENY_XYZ", model=model, known_tools={"file_write"})
        blob = str(model.invocations[0]["messages"])
        assert "POLICY_DENY_XYZ" in blob
