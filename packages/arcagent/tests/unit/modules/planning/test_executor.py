"""Unit tests for the StepExecutor seam (SPEC-040 T-040..T-042)."""

from __future__ import annotations

from typing import Any

import pytest
from arcrun.types import LoopResult

from arcagent.modules.planning.executor import (
    ArcRunStepExecutor,
    StepExecutor,
    StepOutcome,
)
from arcagent.modules.planning.models import (
    Plan,
    PlanBudget,
    PlanStatus,
    PlanStep,
    StepStatus,
)


def _plan(steps: int = 2, max_tokens: int | None = 1000) -> Plan:
    return Plan(
        plan_id="p",
        goal="g",
        goal_source_did="did:arc:user",
        parent_goal_hash="h",
        status=PlanStatus.ACTIVE,
        steps=[PlanStep(step_id=f"s{i}", description=f"do {i}") for i in range(steps)],
        max_replans=2,
        budget=PlanBudget(max_tokens=max_tokens),
    )


def _loop_result(
    *,
    content: str | None = "done",
    completion_payload: dict[str, Any] | None = None,
    completion_tool: str | None = None,
) -> LoopResult:
    return LoopResult(
        content=content,
        turns=1,
        tool_calls_made=0,
        tokens_used={"input": 1, "output": 1, "total": 2},
        strategy_used="react",
        cost_usd=0.0,
        events=[],
        completion_payload=completion_payload,
        completion_tool=completion_tool,
    )


class _RecordingRunFn:
    """Captures the kwargs each run receives; returns a scripted LoopResult."""

    def __init__(self, result: LoopResult | None = None, raises: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = result or _loop_result()
        self._raises = raises

    async def __call__(self, **kwargs: Any) -> LoopResult:
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return self._result


class TestProtocol:
    def test_arcrun_executor_satisfies_protocol(self) -> None:
        ex = ArcRunStepExecutor(_RecordingRunFn())
        assert isinstance(ex, StepExecutor)


class TestClassification:
    @pytest.mark.asyncio
    async def test_clean_run_succeeds(self) -> None:
        run_fn = _RecordingRunFn(_loop_result(content="the answer"))
        ex = ArcRunStepExecutor(run_fn)
        plan = _plan()
        out = await ex.run_step(plan.steps[0], plan=plan)
        assert out.status is StepStatus.SUCCEEDED
        assert out.result == "the answer"

    @pytest.mark.asyncio
    async def test_budget_breach_fails(self) -> None:
        breach = _loop_result(
            content=None,
            completion_payload={"status": "failed", "summary": "cost", "error": "max_cost"},
            completion_tool=None,
        )
        ex = ArcRunStepExecutor(_RecordingRunFn(breach))
        plan = _plan()
        out = await ex.run_step(plan.steps[0], plan=plan)
        assert out.status is StepStatus.FAILED
        assert "max_cost" in (out.failure_reason or "")

    @pytest.mark.asyncio
    async def test_tool_reported_failure_fails(self) -> None:
        failed = _loop_result(
            content=None,
            completion_payload={"status": "failed", "summary": "denied by policy", "error": "denied"},
            completion_tool="task_complete",
        )
        ex = ArcRunStepExecutor(_RecordingRunFn(failed))
        plan = _plan()
        out = await ex.run_step(plan.steps[0], plan=plan)
        assert out.status is StepStatus.FAILED
        assert "denied" in (out.failure_reason or "")

    @pytest.mark.asyncio
    async def test_run_exception_is_captured_not_raised(self) -> None:
        ex = ArcRunStepExecutor(_RecordingRunFn(raises=RuntimeError("boom")))
        plan = _plan()
        out = await ex.run_step(plan.steps[0], plan=plan)
        assert out.status is StepStatus.FAILED
        assert "boom" in (out.failure_reason or "")

    @pytest.mark.asyncio
    async def test_tool_success_terminator_succeeds(self) -> None:
        ok = _loop_result(
            content=None,
            completion_payload={"status": "success", "summary": "did it"},
            completion_tool="task_complete",
        )
        ex = ArcRunStepExecutor(_RecordingRunFn(ok))
        plan = _plan()
        out = await ex.run_step(plan.steps[0], plan=plan)
        assert out.status is StepStatus.SUCCEEDED
        assert out.result == "did it"


class TestAggregateBudget:
    @pytest.mark.asyncio
    async def test_step_capped_at_remaining_aggregate_and_actor(self) -> None:
        run_fn = _RecordingRunFn()
        ex = ArcRunStepExecutor(run_fn, actor_did="did:arc:agent")
        plan = _plan(steps=4, max_tokens=1000)
        plan.tokens_spent = 400  # earlier steps already consumed 400
        await ex.run_step(plan.steps[0], plan=plan)
        call = run_fn.calls[0]
        assert call["task"] == "do 0"
        assert call["max_tokens"] == 600  # remaining aggregate: 1000 - 400
        assert call["actor_did"] == "did:arc:agent"

    @pytest.mark.asyncio
    async def test_unbounded_budget_passes_none(self) -> None:
        run_fn = _RecordingRunFn()
        ex = ArcRunStepExecutor(run_fn)
        plan = _plan(steps=2, max_tokens=None)
        await ex.run_step(plan.steps[0], plan=plan)
        assert run_fn.calls[0]["max_tokens"] is None

    @pytest.mark.asyncio
    async def test_exhausted_budget_fails_without_running(self) -> None:
        run_fn = _RecordingRunFn()
        ex = ArcRunStepExecutor(run_fn)
        plan = _plan(steps=2, max_tokens=1000)
        plan.tokens_spent = 1000  # aggregate already spent
        out = await ex.run_step(plan.steps[0], plan=plan)
        assert out.status is StepStatus.FAILED
        assert "plan budget exhausted" in (out.failure_reason or "")
        assert run_fn.calls == []  # fail-closed: no run was even started

    @pytest.mark.asyncio
    async def test_outcome_reports_consumption(self) -> None:
        run_fn = _RecordingRunFn(
            _loop_result(content="ok")  # tokens_used total=2, cost 0.0
        )
        ex = ArcRunStepExecutor(run_fn)
        plan = _plan(steps=2, max_tokens=1000)
        out = await ex.run_step(plan.steps[0], plan=plan)
        assert out.tokens_used == 2

    @pytest.mark.asyncio
    async def test_attempts_incremented(self) -> None:
        ex = ArcRunStepExecutor(_RecordingRunFn())
        plan = _plan()
        await ex.run_step(plan.steps[0], plan=plan)
        assert plan.steps[0].attempts == 1


class TestOutcomeType:
    def test_outcome_is_dataclass(self) -> None:
        out = StepOutcome(StepStatus.SUCCEEDED, result="x")
        assert out.status is StepStatus.SUCCEEDED
        assert out.result == "x"
        assert out.failure_reason is None


class TestBuildArcrunRunFnForwardsLoopControls:
    """SPEC-043 F2 — a plan-step run is gated identically to the main loop.

    ``build_arcrun_run_fn`` binds directly to ``arcrun.run`` (bypassing
    ``dispatch_stream``). Before the fix it forwarded NO loop controls, so a
    federal-flagged tool inside a plan branch ran un-gated when a
    ``ConcurrentStepExecutor`` was injected. The controls now live in the run_fn
    closure, so BOTH the sequential and concurrent executors inherit them.
    """

    @pytest.mark.asyncio
    async def test_run_fn_forwards_approval_set_breakers_and_checkpoint(self) -> None:
        from unittest.mock import patch

        from arcagent.modules.planning.executor import build_arcrun_run_fn

        captured: dict[str, Any] = {}

        async def _fake_run(*args: Any, **kwargs: Any) -> LoopResult:
            captured.update(kwargs)
            return _loop_result()

        async def _provider(_tc: Any) -> None:
            return None  # deny — no grant

        def _hook(_cp: Any) -> None:
            return None

        with patch("arcrun.run", side_effect=_fake_run):
            run_fn = build_arcrun_run_fn(
                model=object(),
                capabilities=object(),
                system_prompt="sp",
                approval_provider=_provider,
                approval_required_tools=frozenset({"send_email"}),
                max_repeat=8,
                max_consecutive_errors=5,
                on_checkpoint=_hook,
            )
            await run_fn(task="t", max_tokens=100, max_cost_usd=None, actor_did="did:arc:x")

        # A federal-flagged tool in a plan step is gated: the approval set + a
        # bound provider reach arcrun.run, plus the runaway/cascade breakers.
        assert captured["approval_required_tools"] == frozenset({"send_email"})
        assert captured["approval_provider"] is _provider
        assert captured["max_repeat"] == 8
        assert captured["max_consecutive_errors"] == 5
        assert captured["on_checkpoint"] is _hook

    @pytest.mark.asyncio
    async def test_concurrent_executor_inherits_gated_run_fn(self) -> None:
        """The controls ride the run_fn closure, so the concurrent path is gated too."""
        from unittest.mock import patch

        from arcagent.modules.planning.executor import (
            ConcurrentStepExecutor,
            build_arcrun_run_fn,
        )

        seen: dict[str, Any] = {}

        async def _fake_run(*args: Any, **kwargs: Any) -> LoopResult:
            seen.update(kwargs)
            return _loop_result()

        plan = _plan(steps=1, max_tokens=1000)
        with patch("arcrun.run", side_effect=_fake_run):
            run_fn = build_arcrun_run_fn(
                model=object(),
                capabilities=object(),
                system_prompt="sp",
                approval_required_tools=frozenset({"send_email"}),
            )
            ex = ConcurrentStepExecutor(run_fn, step_max_tokens=100)
            await ex.run_ready(plan.steps, plan=plan)

        assert seen["approval_required_tools"] == frozenset({"send_email"})
