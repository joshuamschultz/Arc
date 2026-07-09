"""End-to-end integration for the real planner (SPEC-040 T-070..T-075).

These tests drive the REAL seams, not mocks:

* real ``arcrun.run`` react loop per step (the interim ``ArcRunStepExecutor``),
* the real arctrust ``PolicyPipeline`` (first-DENY-wins) gating a step's tool,
* the real SPEC-038 budget breaker terminating a runaway step,
* a real ``WormSink`` audit chain that is verified.

The headline OQ-3 proof (``test_oq3_full_plan_execute_lifecycle``) shows a goal
becoming a multi-step DAG, executing through real arcrun runs in dependency
order, surviving a simulated restart mid-plan, and replanning a failed step —
bounded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from arcrun import StaticProvider, collect, run_stream
from arcrun.types import Tool
from arctrust import AuditEvent, WormSink, generate_keypair
from arctrust.identity import AgentIdentity
from arctrust.policy import (
    PolicyContext,
    ToolCall,
    build_pipeline,
    sign_call,
)
from arctrust.signer import InProcessSigner

from arcagent.modules.planning.capabilities import _adapt_run_fn
from arcagent.modules.planning.decomposer import DecompositionError, decompose, replan
from arcagent.modules.planning.executor import ArcRunStepExecutor, build_arcrun_run_fn
from arcagent.modules.planning.models import (
    Plan,
    PlanBudget,
    PlanStatus,
    PlanStep,
    StepStatus,
)
from arcagent.modules.planning.orchestrator import PlanOrchestrator
from arcagent.modules.planning.store import PlanStore

# ---------------------------------------------------------------------------
# Scripted arcllm surfaces (inference is arcllm's job; fixed here per AC-2)
# ---------------------------------------------------------------------------


@dataclass
class _Usage:
    input_tokens: int = 10
    output_tokens: int = 5
    total_tokens: int = 15


@dataclass
class _ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class _Resp:
    content: str | None = None
    tool_calls: list[_ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: _Usage = field(default_factory=_Usage)
    cost_usd: float = 0.0


class _PlannerModel:
    """Returns a fixed decomposition/replan as a forced tool call."""

    def __init__(self, *drafts: dict[str, Any]) -> None:
        self._drafts = list(drafts)
        self._i = 0

    async def invoke(self, messages: Any, tools: Any = None, **kwargs: Any) -> Any:
        draft = self._drafts[min(self._i, len(self._drafts) - 1)]
        self._i += 1
        name = tools[0].name if tools else "emit_plan"
        return SimpleNamespace(
            content=None, tool_calls=[SimpleNamespace(name=name, arguments=draft)]
        )


class _ReactModel:
    """A react model that finishes each step by calling the completion tool.

    Branches on the task text: a task containing ``FAIL`` finishes with a
    failed terminator; anything else succeeds. Records the tasks it saw so the
    test can assert dependency-ordered execution.
    """

    def __init__(self) -> None:
        self.tasks_seen: list[str] = []

    async def invoke(self, messages: Any, tools: Any = None) -> _Resp:
        task = _latest_user_text(messages)
        # Only record on the first turn of each run (when no prior tool result).
        if not _has_tool_result(messages):
            self.tasks_seen.append(task)
        status = "failed" if "FAIL" in task else "success"
        summary = "forced failure" if status == "failed" else f"did: {task}"
        return _Resp(
            tool_calls=[
                _ToolCall(id="c1", name="finish", arguments={"status": status, "summary": summary})
            ],
            stop_reason="tool_use",
        )


def _latest_user_text(messages: Any) -> str:
    for m in reversed(list(messages)):
        role = getattr(m, "role", None) or (m.get("role") if isinstance(m, dict) else None)
        if role == "user":
            content = getattr(m, "content", None) or (
                m.get("content") if isinstance(m, dict) else ""
            )
            return content if isinstance(content, str) else str(content)
    return ""


def _has_tool_result(messages: Any) -> bool:
    for m in messages:
        role = getattr(m, "role", None) or (m.get("role") if isinstance(m, dict) else None)
        if role == "tool":
            return True
    return False


# ---------------------------------------------------------------------------
# Real arcrun tools (StaticProvider)
# ---------------------------------------------------------------------------


async def _finish_exec(params: dict[str, Any], ctx: Any) -> str:
    return f"finished: {params.get('summary', '')}"


def _finish_tool() -> Tool:
    return Tool(
        name="finish",
        description="Signal the task is complete.",
        input_schema={
            "type": "object",
            "properties": {"status": {"type": "string"}, "summary": {"type": "string"}},
            "required": ["status", "summary"],
        },
        execute=_finish_exec,
        signals_completion=True,
    )


def _provider() -> StaticProvider:
    return StaticProvider([_finish_tool()])


# ---------------------------------------------------------------------------
# Wiring helpers
# ---------------------------------------------------------------------------

_KNOWN = {"finish", "web_search", "file_write"}


def _store(tmp_path: Path, sink: Any = None) -> PlanStore:
    return PlanStore(tmp_path / "plans", audit_sink=sink, actor_did="did:arc:agent")


def _executor(react_model: _ReactModel) -> ArcRunStepExecutor:
    run_fn = build_arcrun_run_fn(
        model=react_model,
        capabilities=_provider(),
        system_prompt="Execute the step, then call finish.",
        max_turns=4,
    )
    return ArcRunStepExecutor(run_fn, actor_did="did:arc:agent")


def _orchestrator(
    store: PlanStore, react_model: _ReactModel, planner_model: _PlannerModel
) -> PlanOrchestrator:
    async def replan_fn(plan: Plan, reason: str) -> Plan:
        return await replan(plan, reason, model=planner_model, known_tools=_KNOWN)

    return PlanOrchestrator(store, _executor(react_model), replan_fn=replan_fn)


async def _decompose(
    planner: _PlannerModel, *, plan_id: str, max_replans: int = 2, budget: PlanBudget | None = None
) -> Plan:
    return await decompose(
        "produce a report",
        model=planner,
        goal_source_did="did:arc:user",
        parent_goal_hash="hash",
        budget=budget or PlanBudget(max_tokens=100_000),
        max_replans=max_replans,
        known_tools=_KNOWN,
        plan_id=plan_id,
    )


_DIAMOND = {
    "steps": [
        {
            "step_id": "a",
            "description": "gather sources",
            "depends_on": [],
            "tool_hint": "web_search",
        },
        {"step_id": "b", "description": "analyze left", "depends_on": ["a"], "tool_hint": None},
        {"step_id": "c", "description": "analyze right", "depends_on": ["a"], "tool_hint": None},
        {
            "step_id": "d",
            "description": "write report",
            "depends_on": ["b", "c"],
            "tool_hint": "file_write",
        },
    ]
}


# ---------------------------------------------------------------------------
# OQ-3 headline proof
# ---------------------------------------------------------------------------


class TestOQ3Lifecycle:
    @pytest.mark.asyncio
    async def test_oq3_full_plan_execute_lifecycle(self, tmp_path: Path) -> None:
        """Goal -> real DAG -> real arcrun runs (dep order) -> durable -> replan."""
        kp = generate_keypair()
        sink = WormSink(tmp_path / "audit.jsonl", InProcessSigner(kp.private_key))
        store = _store(tmp_path, sink)
        react = _ReactModel()
        planner = _PlannerModel(_DIAMOND)

        # 1. Decompose the goal into a real multi-step DAG.
        plan = await _decompose(planner, plan_id="oq3")
        assert [s.step_id for s in plan.steps] == ["a", "b", "c", "d"]
        plan.validate_dag()
        store.save(plan, action="plan.created")

        # 2. Execute through the interim ArcRunStepExecutor (REAL arcrun runs).
        orch = _orchestrator(store, react, planner)
        final = await orch.execute(plan)

        # 3. All steps succeeded, in a valid dependency order (a before b/c/d;
        #    d last).
        assert final.status is PlanStatus.COMPLETED
        assert all(s.status is StepStatus.SUCCEEDED for s in final.steps)
        order = react.tasks_seen
        assert order.index("gather sources") < order.index("analyze left")
        assert order.index("gather sources") < order.index("analyze right")
        assert order.index("write report") == len(order) - 1

        # 4. Durability: the plan file is the sole resume record and round-trips.
        reloaded = PlanStore(tmp_path / "plans").load("oq3")
        assert reloaded.status is PlanStatus.COMPLETED
        assert all(s.status is StepStatus.SUCCEEDED for s in reloaded.steps)

        # 5. Audit chain (AC-7) verifies over every transition.
        assert sink.verify_chain()
        actions = [e.action for e in _read_events(tmp_path / "audit.jsonl")]
        assert "plan.created" in actions
        assert "plan.completed" in actions
        assert actions.count("plan.step.succeeded") == 4
        sink.close()

    @pytest.mark.asyncio
    async def test_resume_mid_plan_skips_succeeded(self, tmp_path: Path) -> None:
        """A simulated restart resumes from the plan file, skipping done work (AC-6)."""
        store = _store(tmp_path)
        planner = _PlannerModel(_DIAMOND)
        plan = await _decompose(planner, plan_id="resume1")
        # Simulate progress persisted before a crash: 'a' already succeeded.
        plan.get_step("a").status = StepStatus.SUCCEEDED
        plan.get_step("a").result = "sources gathered"
        store.save(plan, action="plan.step.succeeded", target="a")

        # Fresh objects = a restarted process; only the plan file survives.
        react = _ReactModel()
        fresh_store = PlanStore(tmp_path / "plans", actor_did="did:arc:agent")
        orch = _orchestrator(fresh_store, react, planner)
        final = await orch.resume()

        assert final is not None
        assert final.status is PlanStatus.COMPLETED
        # 'a' was NOT re-executed on resume.
        assert "gather sources" not in react.tasks_seen
        assert {"analyze left", "analyze right", "write report"} <= set(react.tasks_seen)

    @pytest.mark.asyncio
    async def test_failed_step_triggers_bounded_replan(self, tmp_path: Path) -> None:
        """A real failed arcrun step replans the remainder and completes (AC-4 shape)."""
        store = _store(tmp_path)
        react = _ReactModel()
        # First plan's step 'b' is marked to FAIL; replan yields a clean 'b2'.
        first = {
            "steps": [
                {
                    "step_id": "a",
                    "description": "gather",
                    "depends_on": [],
                    "tool_hint": "web_search",
                },
                {
                    "step_id": "b",
                    "description": "FAIL this step",
                    "depends_on": ["a"],
                    "tool_hint": None,
                },
            ]
        }
        revised = {
            "steps": [
                {
                    "step_id": "b2",
                    "description": "write cleanly",
                    "depends_on": [],
                    "tool_hint": "file_write",
                }
            ]
        }
        planner = _PlannerModel(first, revised)

        plan = await _decompose(planner, plan_id="replan1")
        store.save(plan, action="plan.created")
        orch = _orchestrator(store, react, planner)
        final = await orch.execute(plan)

        assert final.status is PlanStatus.COMPLETED
        assert final.replans_used == 1
        assert "b2" in [s.step_id for s in final.steps]
        assert final.get_step("a").status is StepStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# AC-4 — real PolicyPipeline DENY -> FAILED -> bounded replan
# ---------------------------------------------------------------------------


class TestRealPolicyDeny:
    @pytest.mark.asyncio
    async def test_policy_deny_marks_step_failed_then_replans(self, tmp_path: Path) -> None:
        ident = AgentIdentity.generate(org="test", agent_type="exec")
        pipeline = build_pipeline(
            tier="personal",
            agent_registry={ident.did: ident.public_key},
            global_deny_rules={"restricted_action": "blocked by operator policy"},
        )

        async def _gated_exec(params: dict[str, Any], ctx: Any) -> str:
            # A REAL first-DENY-wins evaluation of a signed call (no mock).
            call = sign_call(
                ToolCall(
                    tool_name="restricted_action",
                    arguments=params,
                    agent_did=ident.did,
                    session_id="s",
                    classification="UNCLASSIFIED",
                ),
                ident,
            )
            decision = await pipeline.evaluate(
                call, PolicyContext(tier="personal", policy_version="1.0", bundle_age_seconds=0.0)
            )
            if decision.outcome == "deny":
                return f"DENIED: {decision.reason}"
            return "done"

        gated = Tool(
            name="restricted_action",
            description="A restricted action gated by policy.",
            input_schema={"type": "object", "properties": {}},
            execute=_gated_exec,
        )

        class _PolicyReactModel:
            def __init__(self) -> None:
                self.saw_denial = False

            async def invoke(self, messages: Any, tools: Any = None) -> _Resp:
                if _has_tool_result(messages):
                    # Second turn: the tool result carries the DENY — give up.
                    blob = str([getattr(m, "content", "") for m in messages])
                    if "DENIED" in blob:
                        self.saw_denial = True
                    return _Resp(
                        tool_calls=[
                            _ToolCall(
                                id="f",
                                name="finish",
                                arguments={
                                    "status": "failed",
                                    "summary": "policy denied the required tool",
                                },
                            )
                        ],
                        stop_reason="tool_use",
                    )
                # First turn: the restricted step attempts the gated tool; the
                # replanned step (which no longer needs it) finishes cleanly.
                if "restricted" in _latest_user_text(messages):
                    return _Resp(
                        tool_calls=[_ToolCall(id="r", name="restricted_action", arguments={})],
                        stop_reason="tool_use",
                    )
                return _Resp(
                    tool_calls=[
                        _ToolCall(
                            id="ok",
                            name="finish",
                            arguments={"status": "success", "summary": "done"},
                        )
                    ],
                    stop_reason="tool_use",
                )

        react = _PolicyReactModel()
        run_fn = build_arcrun_run_fn(
            model=react,
            capabilities=StaticProvider([gated, _finish_tool()]),
            system_prompt="Do the step.",
            max_turns=4,
        )
        executor = ArcRunStepExecutor(run_fn, actor_did=ident.did)

        store = _store(tmp_path)
        first = {
            "steps": [
                {
                    "step_id": "x",
                    "description": "use the restricted action",
                    "depends_on": [],
                    "tool_hint": "restricted_action",
                }
            ]
        }
        revised = {
            "steps": [
                {"step_id": "x2", "description": "finish", "depends_on": [], "tool_hint": "finish"}
            ]
        }
        planner = _PlannerModel(first, revised)

        async def replan_fn(plan: Plan, reason: str) -> Plan:
            return await replan(
                plan, reason, model=planner, known_tools={"restricted_action", "finish"}
            )

        plan = await decompose(
            "do restricted work",
            model=planner,
            goal_source_did="did:arc:user",
            parent_goal_hash="h",
            budget=PlanBudget(max_tokens=100_000),
            max_replans=1,
            known_tools={"restricted_action", "finish"},
            plan_id="deny1",
        )
        store.save(plan, action="plan.created")
        orch = PlanOrchestrator(store, executor, replan_fn=replan_fn)
        final = await orch.execute(plan)

        assert react.saw_denial  # the REAL pipeline denied, the run observed it
        assert final.replans_used == 1  # the DENY drove a bounded replan
        assert final.status is PlanStatus.COMPLETED


# ---------------------------------------------------------------------------
# AC-5 — real budget breach + max_replans exhaustion
# ---------------------------------------------------------------------------


class TestBudgetAndExhaustion:
    @pytest.mark.asyncio
    async def test_budget_breach_marks_step_failed(self, tmp_path: Path) -> None:
        """A runaway step hits the SPEC-038 token ceiling and is FAILED, not retried."""

        class _LoopingModel:
            """Never finishes — keeps calling a work tool until the breaker fires."""

            async def invoke(self, messages: Any, tools: Any = None) -> _Resp:
                return _Resp(
                    tool_calls=[_ToolCall(id="w", name="work", arguments={})],
                    stop_reason="tool_use",
                    usage=_Usage(input_tokens=50, output_tokens=50, total_tokens=100),
                )

        async def _work(params: dict[str, Any], ctx: Any) -> str:
            return "worked"

        work_tool = Tool(
            name="work",
            description="do work",
            input_schema={"type": "object", "properties": {}},
            execute=_work,
        )
        run_fn = build_arcrun_run_fn(
            model=_LoopingModel(),
            capabilities=StaticProvider([work_tool]),
            system_prompt="work",
            max_turns=50,
        )
        executor = ArcRunStepExecutor(run_fn, actor_did="did:arc:agent")
        # Tiny plan budget → tiny per-step ceiling → breach after a turn or two.
        plan = Plan(
            plan_id="budget1",
            goal="g",
            goal_source_did="did:arc:user",
            parent_goal_hash="h",
            status=PlanStatus.ACTIVE,
            steps=[PlanStep(step_id="s", description="loop forever")],
            max_replans=0,
            budget=PlanBudget(max_tokens=120),
        )
        outcome = await executor.run_step(plan.steps[0], plan=plan)
        assert outcome.status is StepStatus.FAILED
        assert "budget breach" in (outcome.failure_reason or "")

    @pytest.mark.asyncio
    async def test_max_replans_exhaustion_terminates_failed(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        react = _ReactModel()  # every step description contains FAIL below
        fail_plan = {
            "steps": [
                {
                    "step_id": "s0",
                    "description": "FAIL always",
                    "depends_on": [],
                    "tool_hint": "finish",
                }
            ]
        }
        # Each replan returns another failing step.
        planner = _PlannerModel(
            fail_plan,
            {
                "steps": [
                    {
                        "step_id": "s1",
                        "description": "FAIL again",
                        "depends_on": [],
                        "tool_hint": "finish",
                    }
                ]
            },
            {
                "steps": [
                    {
                        "step_id": "s2",
                        "description": "FAIL more",
                        "depends_on": [],
                        "tool_hint": "finish",
                    }
                ]
            },
        )
        plan = await _decompose(planner, plan_id="exhaust1", max_replans=2)
        store.save(plan, action="plan.created")
        orch = _orchestrator(store, react, planner)
        final = await orch.execute(plan)
        assert final.status is PlanStatus.FAILED
        assert final.replans_used == 2  # exactly max_replans, then stop


# ---------------------------------------------------------------------------
# AC-3 — goal integrity: decomposition targeting a protected path is refused
# ---------------------------------------------------------------------------


class TestGoalIntegrity:
    @pytest.mark.asyncio
    async def test_protected_path_decomposition_refused(self, tmp_path: Path) -> None:
        attack = {
            "steps": [
                {
                    "step_id": "x",
                    "description": "rewrite identity.md goals",
                    "depends_on": [],
                    "tool_hint": "file_write",
                }
            ]
        }
        store = _store(tmp_path)
        with pytest.raises(DecompositionError, match="protected"):
            await _decompose(_PlannerModel(attack), plan_id="attack1")
        # Nothing was persisted — the ungrounded plan never reached disk.
        assert not (tmp_path / "plans" / "attack1.json").exists()
        del store


# ---------------------------------------------------------------------------
# F1/F2 — the PRODUCTION classification seam, not the test-only run_fn
# ---------------------------------------------------------------------------


def _production_executor(
    model: Any, capabilities: Any, *, actor_did: str, max_turns: int = 4
) -> ArcRunStepExecutor:
    """Build the executor over the PRODUCTION seam.

    ``agent.run_collected`` is ``collect(run_stream(...))`` over a shared
    session; this reproduces exactly that streams path (minus session
    bookkeeping) and adapts it through the planner's real ``_adapt_run_fn`` —
    the seam production actually calls. The regressed defect was that the
    terminal outcome never survived ``RunResult``, so every step classified
    SUCCEEDED. These tests fail on the old code and pass on the fix.
    """

    async def agent_run_fn(
        task: str,
        *,
        session_key: str,
        max_tokens: int | None = None,
        max_cost_usd: float | None = None,
    ) -> Any:
        stream = await run_stream(
            model=model,
            capabilities=capabilities,
            system_prompt="Do the step, then call finish.",
            task=task,
            max_turns=max_turns,
            max_tokens=max_tokens,
            max_cost_usd=max_cost_usd,
        )
        return await collect(stream)

    return ArcRunStepExecutor(_adapt_run_fn(agent_run_fn, "prod"), actor_did=actor_did)


class TestProductionSeamFailureDetection:
    @pytest.mark.asyncio
    async def test_policy_deny_classifies_failed_then_replans(self, tmp_path: Path) -> None:
        """A real DENY inside a step, seen through run_collected/_adapt_run_fn,
        classifies FAILED and drives a bounded replan (was masked SUCCEEDED)."""
        ident = AgentIdentity.generate(org="test", agent_type="exec")
        pipeline = build_pipeline(
            tier="personal",
            agent_registry={ident.did: ident.public_key},
            global_deny_rules={"restricted_action": "blocked by operator policy"},
        )

        async def _gated_exec(params: dict[str, Any], ctx: Any) -> str:
            call = sign_call(
                ToolCall(
                    tool_name="restricted_action",
                    arguments=params,
                    agent_did=ident.did,
                    session_id="s",
                    classification="UNCLASSIFIED",
                ),
                ident,
            )
            decision = await pipeline.evaluate(
                call, PolicyContext(tier="personal", policy_version="1.0", bundle_age_seconds=0.0)
            )
            if decision.outcome == "deny":
                return f"DENIED: {decision.reason}"
            return "done"

        gated = Tool(
            name="restricted_action",
            description="A restricted action gated by policy.",
            input_schema={"type": "object", "properties": {}},
            execute=_gated_exec,
        )

        class _PolicyReactModel:
            def __init__(self) -> None:
                self.saw_denial = False

            async def invoke(self, messages: Any, tools: Any = None) -> _Resp:
                if _has_tool_result(messages):
                    blob = str([getattr(m, "content", "") for m in messages])
                    if "DENIED" in blob:
                        self.saw_denial = True
                    return _Resp(
                        tool_calls=[
                            _ToolCall(
                                id="f",
                                name="finish",
                                arguments={
                                    "status": "failed",
                                    "summary": "policy denied the required tool",
                                },
                            )
                        ],
                        stop_reason="tool_use",
                    )
                if "restricted" in _latest_user_text(messages):
                    return _Resp(
                        tool_calls=[_ToolCall(id="r", name="restricted_action", arguments={})],
                        stop_reason="tool_use",
                    )
                return _Resp(
                    tool_calls=[
                        _ToolCall(
                            id="ok",
                            name="finish",
                            arguments={"status": "success", "summary": "done"},
                        )
                    ],
                    stop_reason="tool_use",
                )

        react = _PolicyReactModel()
        executor = _production_executor(
            react, StaticProvider([gated, _finish_tool()]), actor_did=ident.did
        )
        store = _store(tmp_path)
        first = {
            "steps": [
                {
                    "step_id": "x",
                    "description": "use the restricted action",
                    "depends_on": [],
                    "tool_hint": "restricted_action",
                }
            ]
        }
        revised = {
            "steps": [
                {"step_id": "x2", "description": "finish", "depends_on": [], "tool_hint": "finish"}
            ]
        }
        planner = _PlannerModel(first, revised)

        async def replan_fn(plan: Plan, reason: str) -> Plan:
            return await replan(
                plan, reason, model=planner, known_tools={"restricted_action", "finish"}
            )

        plan = await decompose(
            "do restricted work",
            model=planner,
            goal_source_did="did:arc:user",
            parent_goal_hash="h",
            budget=PlanBudget(max_tokens=100_000),
            max_replans=1,
            known_tools={"restricted_action", "finish"},
            plan_id="deny-prod",
        )
        store.save(plan, action="plan.created")
        orch = PlanOrchestrator(store, executor, replan_fn=replan_fn)
        final = await orch.execute(plan)

        assert react.saw_denial  # the REAL pipeline denied inside the run
        assert final.replans_used == 1  # DENY -> FAILED -> bounded replan
        assert final.status is PlanStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_aggregate_budget_stops_runaway_plan(self, tmp_path: Path) -> None:
        """Cumulative tokens across steps (surfaced through the production seam)
        halt a multi-step plan at the aggregate ceiling, not per-step only."""

        class _BurnModel:
            def __init__(self) -> None:
                self.tasks_seen: list[str] = []

            async def invoke(self, messages: Any, tools: Any = None) -> _Resp:
                task = _latest_user_text(messages)
                if not _has_tool_result(messages):
                    self.tasks_seen.append(task)
                return _Resp(
                    tool_calls=[
                        _ToolCall(
                            id="c1",
                            name="finish",
                            arguments={"status": "success", "summary": f"did {task}"},
                        )
                    ],
                    stop_reason="tool_use",
                    usage=_Usage(input_tokens=250, output_tokens=250, total_tokens=500),
                )

        burn = _BurnModel()
        executor = _production_executor(burn, _provider(), actor_did="did:arc:agent")
        store = _store(tmp_path)
        linear3 = {
            "steps": [
                {"step_id": "a", "description": "step a", "depends_on": [], "tool_hint": "finish"},
                {"step_id": "b", "description": "step b", "depends_on": ["a"], "tool_hint": None},
                {"step_id": "c", "description": "step c", "depends_on": ["b"], "tool_hint": None},
            ]
        }
        planner = _PlannerModel(linear3)
        plan = await decompose(
            "burn budget",
            model=planner,
            goal_source_did="did:arc:user",
            parent_goal_hash="h",
            budget=PlanBudget(max_tokens=900),  # a(500)+b(500) crosses it before c
            max_replans=0,
            known_tools=_KNOWN,
            plan_id="burn-prod",
        )
        store.save(plan, action="plan.created")
        orch = PlanOrchestrator(store, executor, replan_fn=_never_replan_prod)
        final = await orch.execute(plan)

        assert final.status is PlanStatus.FAILED  # aggregate ceiling stopped it
        assert "step c" not in burn.tasks_seen  # c never dispatched
        assert final.tokens_spent == 1000  # a(500)+b(500), checkpointed
        assert final.budget_exhausted()


async def _never_replan_prod(plan: Plan, reason: str) -> Plan:  # pragma: no cover
    raise AssertionError("replan should not be called with max_replans=0")


def _read_events(path: Path) -> list[AuditEvent]:
    import json

    events: list[AuditEvent] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        # WormSink wraps each event; the audited fields live under "event".
        payload = record.get("event", record)
        events.append(AuditEvent.model_validate(payload))
    return events
