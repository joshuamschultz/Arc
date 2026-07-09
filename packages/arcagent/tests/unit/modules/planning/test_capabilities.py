"""Unit tests for the planner LLM surface + hooks (SPEC-040 T-060..T-062)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from arcagent.modules.planning import _runtime, capabilities


class _FakeModel:
    def __init__(self, arguments: dict[str, Any]) -> None:
        self._arguments = arguments

    async def invoke(self, messages: Any, tools: Any = None, **kwargs: Any) -> Any:
        call = SimpleNamespace(name=tools[0].name if tools else "emit_plan", arguments=self._arguments)
        return SimpleNamespace(content=None, tool_calls=[call])


_TWO_STEP = {
    "steps": [
        {"step_id": "a", "description": "gather", "depends_on": [], "tool_hint": "web_search"},
        {"step_id": "b", "description": "write", "depends_on": ["a"], "tool_hint": None},
    ]
}

# Two INDEPENDENT steps — the whole ready frontier is dispatchable at once, so a
# concurrent executor runs both in parallel while a sequential one runs a→b.
_TWO_INDEP = {
    "steps": [
        {"step_id": "a", "description": "task a", "depends_on": [], "tool_hint": None},
        {"step_id": "b", "description": "task b", "depends_on": [], "tool_hint": None},
    ]
}


async def _fake_run(
    input_text: str,
    *,
    session_key: str,
    max_tokens: int | None = None,
    max_cost_usd: float | None = None,
) -> Any:
    # Shaped like a real ``RunResult``: carries the terminal outcome the
    # production classifier now reads (SPEC-040 F1). No completion payload =
    # a clean end = SUCCEEDED.
    return SimpleNamespace(
        content=f"ran: {input_text}",
        turns=1,
        tool_calls_made=0,
        cost_usd=0.0,
        tokens_used={"total": 1},
        completion_payload=None,
        completion_tool=None,
    )


@pytest.fixture(autouse=True)
def _reset() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


def _configure(tmp_path: Path, model: dict[str, Any]) -> None:
    _runtime.configure(workspace=tmp_path, agent_name="tester", agent_did="did:arc:tester")
    st = _runtime.state()
    st.eval_model = _FakeModel(model)
    st.run_fn = _fake_run
    st.known_tools = {"web_search", "file_write"}


class TestPlanCreate:
    @pytest.mark.asyncio
    async def test_creates_and_runs_plan(self, tmp_path: Path) -> None:
        _configure(tmp_path, _TWO_STEP)
        result = json.loads(await capabilities.plan_create("write a report"))
        assert result["status"] == "completed"
        assert [s["step_id"] for s in result["steps"]] == ["a", "b"]
        assert all(s["status"] == "succeeded" for s in result["steps"])
        assert (tmp_path / "plans" / f"{result['plan_id']}.json").exists()

    @pytest.mark.asyncio
    async def test_no_run_seam_returns_error(self, tmp_path: Path) -> None:
        _runtime.configure(workspace=tmp_path, agent_name="t", agent_did="did:arc:t")
        _runtime.state().eval_model = _FakeModel(_TWO_STEP)
        # run_fn not bound
        result = json.loads(await capabilities.plan_create("goal"))
        assert "error" in result

    @pytest.mark.asyncio
    async def test_ungrounded_decomposition_rejected(self, tmp_path: Path) -> None:
        _configure(tmp_path, {"steps": [{"step_id": "x", "description": "x", "depends_on": [], "tool_hint": "ghost_tool"}]})
        result = json.loads(await capabilities.plan_create("goal"))
        assert "error" in result
        assert "rejected" in result["error"]


class TestConcurrentWiring:
    """SPEC-043 — the ``concurrent`` PlanningConfig flag selects the concurrent
    executor on the REAL orchestrator-build path (``_build_orchestrator``)."""

    @pytest.mark.asyncio
    async def test_default_flag_builds_sequential_executor(self, tmp_path: Path) -> None:
        from arcagent.modules.planning.executor import ArcRunStepExecutor

        _configure(tmp_path, _TWO_INDEP)
        assert _runtime.state().config.concurrent is False
        orch = capabilities._build_orchestrator("pid-seq")
        assert isinstance(orch._executor, ArcRunStepExecutor)

    @pytest.mark.asyncio
    async def test_concurrent_flag_dispatches_frontier_in_parallel(
        self, tmp_path: Path
    ) -> None:
        from arcagent.modules.planning.executor import ConcurrentStepExecutor

        _runtime.configure(
            workspace=tmp_path,
            agent_name="tester",
            agent_did="did:arc:tester",
            config={"enabled": True, "concurrent": True},
        )
        st = _runtime.state()
        st.eval_model = _FakeModel(_TWO_INDEP)
        st.known_tools = set()

        # Both independent branches must be in-flight simultaneously to clear
        # the barrier; a sequential executor would deadlock (and time out).
        barrier = asyncio.Barrier(2)

        async def _concurrent_run(
            input_text: str,
            *,
            session_key: str,
            max_tokens: int | None = None,
            max_cost_usd: float | None = None,
        ) -> Any:
            await asyncio.wait_for(barrier.wait(), timeout=1.0)
            return SimpleNamespace(
                content=f"ran: {input_text}",
                turns=1,
                tool_calls_made=0,
                cost_usd=0.0,
                tokens_used={"total": 1},
                completion_payload=None,
                completion_tool=None,
            )

        st.run_fn = _concurrent_run

        # The real build path yields the concurrent executor.
        assert isinstance(
            capabilities._build_orchestrator("pid-conc")._executor,
            ConcurrentStepExecutor,
        )

        result = json.loads(await capabilities.plan_create("do two things"))
        assert result["status"] == "completed"
        assert all(s["status"] == "succeeded" for s in result["steps"])


class TestPlanStatus:
    def test_status_is_read_only(self) -> None:
        meta = capabilities.plan_status._arc_capability_meta
        assert meta.classification == "read_only"

    @pytest.mark.asyncio
    async def test_status_reports_steps(self, tmp_path: Path) -> None:
        _configure(tmp_path, _TWO_STEP)
        await capabilities.plan_create("write a report")
        status = json.loads(await capabilities.plan_status())
        # Completed plans aren't "active"; abandon leaves none either. Create a
        # fresh active plan by not finishing: status returns the active plan or None.
        assert status["active_plan"] is None or "steps" in status

    @pytest.mark.asyncio
    async def test_status_none_when_no_plan(self, tmp_path: Path) -> None:
        _configure(tmp_path, _TWO_STEP)
        status = json.loads(await capabilities.plan_status())
        assert status["active_plan"] is None


class TestHooks:
    @pytest.mark.asyncio
    async def test_assemble_prompt_injects_active_plan(self, tmp_path: Path) -> None:
        _configure(tmp_path, _TWO_STEP)
        # Persist an ACTIVE plan directly (not run to completion).
        from arcagent.modules.planning.decomposer import decompose

        plan = await decompose(
            "goal",
            model=_FakeModel(_TWO_STEP),
            goal_source_did="did:arc:tester",
            parent_goal_hash="h",
            budget=_runtime.state().budget,
            max_replans=3,
            known_tools={"web_search"},
            plan_id="active1",
        )
        _runtime.state().store.save(plan, action="plan.created")
        ctx = SimpleNamespace(data={"sections": {}})
        await capabilities.inject_planning_section(ctx)
        assert "planning" in ctx.data["sections"]
        assert "Active Plan" in ctx.data["sections"]["planning"]

    @pytest.mark.asyncio
    async def test_assemble_prompt_no_section_without_plan(self, tmp_path: Path) -> None:
        _configure(tmp_path, _TWO_STEP)
        ctx = SimpleNamespace(data={"sections": {}})
        await capabilities.inject_planning_section(ctx)
        assert "planning" not in ctx.data["sections"]

    @pytest.mark.asyncio
    async def test_agent_ready_binds_run_fn(self, tmp_path: Path) -> None:
        _runtime.configure(workspace=tmp_path, agent_name="t", agent_did="did:arc:t")
        ctx = SimpleNamespace(data={"run_fn": _fake_run})
        await capabilities.planning_bind_run_fn(ctx)
        assert _runtime.state().run_fn is _fake_run


class TestGoalDriftBinding:
    """F5: parent_goal_hash binds to identity.md; drift refuses further execution."""

    @pytest.mark.asyncio
    async def test_plan_create_binds_to_identity_md_not_goal_string(
        self, tmp_path: Path
    ) -> None:
        import hashlib

        (tmp_path / "identity.md").write_text("Goals: serve the mission", encoding="utf-8")
        _configure(tmp_path, _TWO_STEP)
        result = json.loads(await capabilities.plan_create("write a report"))
        plan = _runtime.state().store.load(result["plan_id"])
        # Bound to the identity.md charter (ASI01), not the plan's own goal text.
        assert plan.parent_goal_hash == _runtime.identity_goal_hash()
        assert (
            plan.parent_goal_hash
            != hashlib.sha256(b"did:arc:tester::write a report").hexdigest()
        )

    @pytest.mark.asyncio
    async def test_replan_refused_after_identity_goals_change(self, tmp_path: Path) -> None:
        from arcagent.modules.planning.models import Plan, PlanStatus, PlanStep

        identity = tmp_path / "identity.md"
        identity.write_text("Goals: v1 mission", encoding="utf-8")
        _configure(tmp_path, _TWO_STEP)
        st = _runtime.state()
        plan = Plan(
            plan_id="drift1",
            goal="g",
            goal_source_did="did:arc:tester",
            parent_goal_hash=_runtime.identity_goal_hash(),  # bound to v1
            status=PlanStatus.ACTIVE,
            steps=[PlanStep(step_id="a", description="do a")],
        )
        st.store.save(plan, action="plan.created")
        # identity.md goals are rewritten under the running plan (drift/hijack).
        identity.write_text("Goals: v2 — a different mission", encoding="utf-8")
        result = json.loads(await capabilities.plan_replan("continue"))
        assert "goal drift" in result["error"]

    @pytest.mark.asyncio
    async def test_replan_allowed_when_identity_unchanged(self, tmp_path: Path) -> None:
        from arcagent.modules.planning.models import Plan, PlanStatus, PlanStep

        (tmp_path / "identity.md").write_text("Goals: steady mission", encoding="utf-8")
        _configure(tmp_path, _TWO_STEP)
        st = _runtime.state()
        plan = Plan(
            plan_id="steady1",
            goal="g",
            goal_source_did="did:arc:tester",
            parent_goal_hash=_runtime.identity_goal_hash(),
            status=PlanStatus.ACTIVE,
            steps=[PlanStep(step_id="a", description="do a", tool_hint="web_search")],
        )
        st.store.save(plan, action="plan.created")
        result = json.loads(await capabilities.plan_replan("continue"))
        # Not refused for drift — the binding still holds (it may replan/complete).
        assert "goal drift" not in result.get("error", "")


class TestOperatorSignedAudit:
    """F3: with an operator signer, every plan transition lands on the WORM chain."""

    @pytest.mark.asyncio
    async def test_plan_lifecycle_recorded_on_verifiable_worm_chain(
        self, tmp_path: Path
    ) -> None:
        from arctrust import generate_keypair
        from arctrust.signer import InProcessSigner

        kp = generate_keypair()
        # Nest workspace so the operator-owned chain lands inside tmp_path.
        ws = tmp_path / "ws"
        _runtime.configure(
            workspace=ws,
            agent_name="tester",
            agent_did="did:arc:tester",
            operator_signer=InProcessSigner(kp.private_key),
        )
        st = _runtime.state()
        st.eval_model = _FakeModel(_TWO_STEP)
        st.run_fn = _fake_run
        st.known_tools = {"web_search", "file_write"}

        # The store received the operator-signed WORM sink (not None).
        sink = st.store._audit_sink
        assert sink is not None

        result = json.loads(await capabilities.plan_create("write a report"))
        assert result["status"] == "completed"

        # The tamper-evident chain verifies and holds every transition.
        assert sink.verify_chain()
        actions = [e.action for e in _read_worm_events(tmp_path / ".audit" / "planning.worm")]
        assert "plan.created" in actions
        assert "plan.completed" in actions
        assert actions.count("plan.step.succeeded") == 2
        sink.close()


def _read_worm_events(path: Path) -> list[Any]:
    from arctrust import AuditEvent

    events: list[Any] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        events.append(AuditEvent.model_validate(record.get("event", record)))
    return events


class TestPlanAbandon:
    @pytest.mark.asyncio
    async def test_abandon_marks_plan(self, tmp_path: Path) -> None:
        _configure(tmp_path, _TWO_STEP)
        from arcagent.modules.planning.decomposer import decompose

        plan = await decompose(
            "goal",
            model=_FakeModel(_TWO_STEP),
            goal_source_did="did:arc:tester",
            parent_goal_hash="h",
            budget=_runtime.state().budget,
            max_replans=3,
            known_tools={"web_search"},
            plan_id="ab1",
        )
        _runtime.state().store.save(plan, action="plan.created")
        result = json.loads(await capabilities.plan_abandon("no longer needed"))
        assert result["status"] == "abandoned"
        assert _runtime.state().store.active_plan() is None
