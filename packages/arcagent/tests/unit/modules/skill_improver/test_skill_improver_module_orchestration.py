"""Unit tests for SkillImproverModule._optimize_skill orchestration.

Coverage targets (R32, M-6):
- _optimize_skill filters traces by trace_buffer_turns
- checks guardrails.check_eligible and exits early on ineligible
- reads skill file / handles OSError gracefully
- emits optimization_started and optimization_completed audit events
- applies result only when best_candidate.id != 'seed'
- updates generation counter after successful apply
- rescans registry after apply
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.modules.skill_improver.models import Candidate, OptimizeResult, SkillTrace
from arcagent.modules.skill_improver.skill_improver_module import SkillImproverModule

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_trace(turn_number: int = 0, skill_name: str = "my-skill") -> SkillTrace:
    from datetime import UTC, datetime

    return SkillTrace(
        trace_id=f"trace-{turn_number}",
        session_id="sess-1",
        skill_name=skill_name,
        skill_version=0,
        turn_number=turn_number,
        started_at=datetime.now(UTC),
    )


def _make_candidate(cid: str = "cand-1", generation: int = 1) -> Candidate:
    c = MagicMock(spec=Candidate)
    c.id = cid
    c.generation = generation
    c.fingerprint = f"fp-{cid}"
    c.aggregate_scores = {}
    return c


def _make_optimize_result(
    skill_name: str = "my-skill",
    best_id: str = "cand-1",
    generation: int = 1,
) -> OptimizeResult:
    r = MagicMock(spec=OptimizeResult)
    r.skill_name = skill_name
    r.best_candidate = _make_candidate(best_id, generation)
    r.seed_scores = {"quality": 0.7}
    r.to_dict = MagicMock(return_value={"skill_name": skill_name})
    return r


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def telemetry() -> MagicMock:
    t = MagicMock()
    t.audit_event = MagicMock()
    return t


@pytest.fixture
def module(workspace: Path, telemetry: MagicMock) -> SkillImproverModule:
    return SkillImproverModule(workspace=workspace, telemetry=telemetry)


def _wire_collector(module: SkillImproverModule, traces: list, turn_number: int = 100) -> MagicMock:
    """Inject a mock trace collector into the module."""
    collector = MagicMock()
    collector.turn_number = turn_number
    collector.load_traces = MagicMock(return_value=traces)
    collector.reset_count = MagicMock()
    collector.index_skills = MagicMock()
    module._collector = collector
    return collector


def _wire_skill_registry(module: SkillImproverModule, skill_name: str, skill_path: Path) -> MagicMock:
    """Inject a mock skill registry pointing skill_name at skill_path."""
    registry = MagicMock()
    skill = MagicMock()
    skill.name = skill_name
    skill.file_path = skill_path
    registry.skills = [skill]
    registry.discover = MagicMock()
    module._skill_registry = registry
    return registry


# ---------------------------------------------------------------------------
# trace_buffer_turns filtering (R32)
# ---------------------------------------------------------------------------


class TestTraceBufferFiltering:
    @pytest.mark.asyncio
    async def test_traces_within_buffer_are_excluded(
        self, module: SkillImproverModule, workspace: Path, telemetry: MagicMock
    ) -> None:
        """Traces within trace_buffer_turns are filtered before eligibility check."""
        skill_name = "my-skill"
        current_turn = 100
        buffer_turns = module._config.trace_buffer_turns  # typically 5

        # One trace is recent (within buffer), one is old enough
        recent_trace = _make_trace(turn_number=current_turn - 1)
        old_trace = _make_trace(turn_number=current_turn - buffer_turns - 1)
        all_traces = [recent_trace, old_trace]

        _wire_collector(module, all_traces, turn_number=current_turn)

        # guardrails should receive only the old trace
        with patch.object(
            module._guardrails,
            "check_eligible",
            return_value=False,  # exit early
        ) as mock_check:
            await module._optimize_skill(skill_name)

        # Verify call included only the eligible (old) trace
        call_args = mock_check.call_args
        traces_passed = call_args.args[1]
        assert old_trace in traces_passed
        assert recent_trace not in traces_passed

    @pytest.mark.asyncio
    async def test_all_traces_within_buffer_exits_early(
        self, module: SkillImproverModule
    ) -> None:
        """When all traces fall within the buffer, check_eligible sees [] and exits."""
        skill_name = "my-skill"
        current_turn = 100
        _buffer_turns = module._config.trace_buffer_turns

        # All traces are too recent
        traces = [_make_trace(turn_number=current_turn - 1) for _ in range(40)]
        _wire_collector(module, traces, turn_number=current_turn)

        with patch.object(
            module._guardrails, "check_eligible", return_value=False
        ) as mock_check:
            await module._optimize_skill(skill_name)

        call_args = mock_check.call_args
        traces_passed = call_args.args[1]
        assert len(traces_passed) == 0


# ---------------------------------------------------------------------------
# guardrails.check_eligible early exit
# ---------------------------------------------------------------------------


class TestGuardrailsEarlyExit:
    @pytest.mark.asyncio
    async def test_ineligible_exits_before_file_read(
        self, module: SkillImproverModule, workspace: Path, telemetry: MagicMock
    ) -> None:
        """When guardrails returns False, optimization_started is never emitted."""
        skill_name = "my-skill"
        skill_file = workspace / "skills" / f"{skill_name}.md"
        skill_file.parent.mkdir(parents=True, exist_ok=True)
        skill_file.write_text("# skill content")

        _wire_collector(module, [_make_trace() for _ in range(50)], turn_number=100)
        _wire_skill_registry(module, skill_name, skill_file)

        with patch.object(module._guardrails, "check_eligible", return_value=False):
            await module._optimize_skill(skill_name)

        # No audit events means optimization pipeline was not entered
        telemetry.audit_event.assert_not_called()


# ---------------------------------------------------------------------------
# File I/O — OSError handled gracefully
# ---------------------------------------------------------------------------


class TestSkillFileHandling:
    @pytest.mark.asyncio
    async def test_missing_skill_file_returns_gracefully(
        self, module: SkillImproverModule, workspace: Path
    ) -> None:
        """OSError reading skill file logs a warning and returns without raising."""
        skill_name = "missing-skill"
        nonexistent = workspace / "skills" / f"{skill_name}.md"

        traces = [_make_trace() for _ in range(50)]
        _wire_collector(module, traces, turn_number=100)
        _wire_skill_registry(module, skill_name, nonexistent)

        with patch.object(module._guardrails, "check_eligible", return_value=True):
            with patch.object(module, "_get_eval_model", return_value=MagicMock()):
                # File does not exist, so read_text raises FileNotFoundError
                await module._optimize_skill(skill_name)
        # Should not raise — graceful return

    @pytest.mark.asyncio
    async def test_no_skill_registry_returns_without_error(
        self, module: SkillImproverModule
    ) -> None:
        """No skill_registry configured means _get_skill_path returns None → graceful exit."""
        skill_name = "any-skill"
        _wire_collector(module, [_make_trace() for _ in range(50)], turn_number=100)

        with patch.object(module._guardrails, "check_eligible", return_value=True):
            await module._optimize_skill(skill_name)
        # No error


# ---------------------------------------------------------------------------
# Audit events (M-6)
# ---------------------------------------------------------------------------


class TestAuditEvents:
    @pytest.mark.asyncio
    async def test_optimization_started_emitted(
        self, module: SkillImproverModule, workspace: Path, telemetry: MagicMock
    ) -> None:
        skill_name = "my-skill"
        skill_file = workspace / "skills" / f"{skill_name}.md"
        skill_file.parent.mkdir(parents=True, exist_ok=True)
        skill_file.write_text("# content")

        _wire_collector(module, [_make_trace() for _ in range(50)], turn_number=100)
        _wire_skill_registry(module, skill_name, skill_file)

        mock_result = _make_optimize_result(skill_name, best_id="seed")

        with patch.object(module._guardrails, "check_eligible", return_value=True):
            with patch.object(module, "_get_eval_model", return_value=MagicMock()):
                with patch(
                    "arcagent.modules.skill_improver.skill_improver_module.SkillEvaluator"
                ):
                    with patch(
                        "arcagent.modules.skill_improver.skill_improver_module.SkillReflector"
                    ):
                        with patch(
                            "arcagent.modules.skill_improver.skill_improver_module.SkillOptimizer"
                        ) as mock_optimizer_cls:
                            mock_optimizer = AsyncMock()
                            mock_optimizer.optimize = AsyncMock(return_value=mock_result)
                            mock_optimizer_cls.return_value = mock_optimizer
                            await module._optimize_skill(skill_name)

        audit_calls = [call.args[0] for call in telemetry.audit_event.call_args_list]
        assert "skill_improver.optimization_started" in audit_calls

    @pytest.mark.asyncio
    async def test_optimization_completed_emitted_on_improvement(
        self, module: SkillImproverModule, workspace: Path, telemetry: MagicMock
    ) -> None:
        skill_name = "my-skill"
        skill_file = workspace / "skills" / f"{skill_name}.md"
        skill_file.parent.mkdir(parents=True, exist_ok=True)
        skill_file.write_text("# content")

        _wire_collector(module, [_make_trace() for _ in range(50)], turn_number=100)
        _registry = _wire_skill_registry(module, skill_name, skill_file)

        # best_candidate.id != "seed" → improvement detected
        mock_result = _make_optimize_result(skill_name, best_id="improved-cand")

        with patch.object(module._guardrails, "check_eligible", return_value=True):
            with patch.object(module, "_get_eval_model", return_value=MagicMock()):
                with patch(
                    "arcagent.modules.skill_improver.skill_improver_module.SkillEvaluator"
                ):
                    with patch(
                        "arcagent.modules.skill_improver.skill_improver_module.SkillReflector"
                    ):
                        with patch(
                            "arcagent.modules.skill_improver.skill_improver_module.SkillOptimizer"
                        ) as mock_optimizer_cls:
                            mock_optimizer = AsyncMock()
                            mock_optimizer.optimize = AsyncMock(return_value=mock_result)
                            mock_optimizer.apply_result = MagicMock()
                            mock_optimizer_cls.return_value = mock_optimizer
                            await module._optimize_skill(skill_name)

        audit_calls = [call.args[0] for call in telemetry.audit_event.call_args_list]
        assert "skill_improver.optimization_completed" in audit_calls


# ---------------------------------------------------------------------------
# best_candidate.id == "seed" → no apply
# ---------------------------------------------------------------------------


class TestNoImprovementLogic:
    @pytest.mark.asyncio
    async def test_seed_result_does_not_call_apply(
        self, module: SkillImproverModule, workspace: Path
    ) -> None:
        skill_name = "my-skill"
        skill_file = workspace / "skills" / f"{skill_name}.md"
        skill_file.parent.mkdir(parents=True, exist_ok=True)
        skill_file.write_text("# content")

        _wire_collector(module, [_make_trace() for _ in range(50)], turn_number=100)
        _wire_skill_registry(module, skill_name, skill_file)

        # best_candidate.id == "seed" → should NOT apply
        mock_result = _make_optimize_result(skill_name, best_id="seed")

        with patch.object(module._guardrails, "check_eligible", return_value=True):
            with patch.object(module, "_get_eval_model", return_value=MagicMock()):
                with patch(
                    "arcagent.modules.skill_improver.skill_improver_module.SkillEvaluator"
                ):
                    with patch(
                        "arcagent.modules.skill_improver.skill_improver_module.SkillReflector"
                    ):
                        with patch(
                            "arcagent.modules.skill_improver.skill_improver_module.SkillOptimizer"
                        ) as mock_optimizer_cls:
                            mock_optimizer = AsyncMock()
                            mock_optimizer.optimize = AsyncMock(return_value=mock_result)
                            mock_optimizer.apply_result = MagicMock()
                            mock_optimizer_cls.return_value = mock_optimizer
                            await module._optimize_skill(skill_name)

        # apply_result must NOT be called when best == seed
        mock_optimizer.apply_result.assert_not_called()

    @pytest.mark.asyncio
    async def test_none_optimize_result_returns_early(
        self, module: SkillImproverModule, workspace: Path
    ) -> None:
        """optimizer.optimize returning None must return without applying anything."""
        skill_name = "my-skill"
        skill_file = workspace / "skills" / f"{skill_name}.md"
        skill_file.parent.mkdir(parents=True, exist_ok=True)
        skill_file.write_text("# content")

        _wire_collector(module, [_make_trace() for _ in range(50)], turn_number=100)
        _wire_skill_registry(module, skill_name, skill_file)

        with patch.object(module._guardrails, "check_eligible", return_value=True):
            with patch.object(module, "_get_eval_model", return_value=MagicMock()):
                with patch(
                    "arcagent.modules.skill_improver.skill_improver_module.SkillEvaluator"
                ):
                    with patch(
                        "arcagent.modules.skill_improver.skill_improver_module.SkillReflector"
                    ):
                        with patch(
                            "arcagent.modules.skill_improver.skill_improver_module.SkillOptimizer"
                        ) as mock_optimizer_cls:
                            mock_optimizer = AsyncMock()
                            mock_optimizer.optimize = AsyncMock(return_value=None)
                            mock_optimizer.apply_result = MagicMock()
                            mock_optimizer_cls.return_value = mock_optimizer
                            await module._optimize_skill(skill_name)

        mock_optimizer.apply_result.assert_not_called()


# ---------------------------------------------------------------------------
# Generation counter update and registry rescan
# ---------------------------------------------------------------------------


class TestPostApplyUpdates:
    @pytest.mark.asyncio
    async def test_generation_counter_updated_after_apply(
        self, module: SkillImproverModule, workspace: Path
    ) -> None:
        skill_name = "my-skill"
        skill_file = workspace / "skills" / f"{skill_name}.md"
        skill_file.parent.mkdir(parents=True, exist_ok=True)
        skill_file.write_text("# content")

        _wire_collector(module, [_make_trace() for _ in range(50)], turn_number=100)
        _registry = _wire_skill_registry(module, skill_name, skill_file)

        new_generation = 3
        mock_result = _make_optimize_result(
            skill_name, best_id="improved-cand", generation=new_generation
        )

        with patch.object(module._guardrails, "check_eligible", return_value=True):
            with patch.object(module._guardrails, "set_generation") as mock_set_gen:
                with patch.object(module, "_get_eval_model", return_value=MagicMock()):
                    with patch(
                        "arcagent.modules.skill_improver.skill_improver_module.SkillEvaluator"
                    ):
                        with patch(
                            "arcagent.modules.skill_improver.skill_improver_module.SkillReflector"
                        ):
                            with patch(
                                "arcagent.modules.skill_improver.skill_improver_module.SkillOptimizer"
                            ) as mock_optimizer_cls:
                                mock_optimizer = AsyncMock()
                                mock_optimizer.optimize = AsyncMock(return_value=mock_result)
                                mock_optimizer.apply_result = MagicMock()
                                mock_optimizer_cls.return_value = mock_optimizer
                                await module._optimize_skill(skill_name)

        mock_set_gen.assert_called_once_with(skill_name, new_generation)

    @pytest.mark.asyncio
    async def test_registry_rescanned_after_apply(
        self, module: SkillImproverModule, workspace: Path
    ) -> None:
        skill_name = "my-skill"
        skill_file = workspace / "skills" / f"{skill_name}.md"
        skill_file.parent.mkdir(parents=True, exist_ok=True)
        skill_file.write_text("# content")

        collector = _wire_collector(module, [_make_trace() for _ in range(50)], turn_number=100)
        registry = _wire_skill_registry(module, skill_name, skill_file)

        mock_result = _make_optimize_result(skill_name, best_id="improved-cand")

        with patch.object(module._guardrails, "check_eligible", return_value=True):
            with patch.object(module, "_get_eval_model", return_value=MagicMock()):
                with patch(
                    "arcagent.modules.skill_improver.skill_improver_module.SkillEvaluator"
                ):
                    with patch(
                        "arcagent.modules.skill_improver.skill_improver_module.SkillReflector"
                    ):
                        with patch(
                            "arcagent.modules.skill_improver.skill_improver_module.SkillOptimizer"
                        ) as mock_optimizer_cls:
                            mock_optimizer = AsyncMock()
                            mock_optimizer.optimize = AsyncMock(return_value=mock_result)
                            mock_optimizer.apply_result = MagicMock()
                            mock_optimizer_cls.return_value = mock_optimizer
                            await module._optimize_skill(skill_name)

        # Registry must have been rescanned
        registry.discover.assert_called_once()
        collector.index_skills.assert_called_once_with(registry)


# ---------------------------------------------------------------------------
# Bus handlers — collector delegation
# ---------------------------------------------------------------------------


class TestBusHandlers:
    @pytest.mark.asyncio
    async def test_on_post_tool_delegates_to_collector(
        self, module: SkillImproverModule
    ) -> None:
        collector = MagicMock()
        collector.on_post_tool = AsyncMock()
        module._collector = collector

        ctx = MagicMock()
        await module._on_post_tool(ctx)
        collector.on_post_tool.assert_called_once_with(ctx)

    @pytest.mark.asyncio
    async def test_on_post_tool_noop_when_no_collector(
        self, module: SkillImproverModule
    ) -> None:
        """_on_post_tool with no collector must not raise."""
        ctx = MagicMock()
        await module._on_post_tool(ctx)  # must not raise

    @pytest.mark.asyncio
    async def test_on_post_plan_delegates_to_collector(
        self, module: SkillImproverModule
    ) -> None:
        collector = MagicMock()
        collector.on_post_plan = AsyncMock()
        module._collector = collector

        ctx = MagicMock()
        await module._on_post_plan(ctx)
        collector.on_post_plan.assert_called_once_with(ctx)

    @pytest.mark.asyncio
    async def test_on_post_plan_noop_when_no_collector(
        self, module: SkillImproverModule
    ) -> None:
        ctx = MagicMock()
        await module._on_post_plan(ctx)  # must not raise

    @pytest.mark.asyncio
    async def test_on_post_respond_noop_when_no_collector(
        self, module: SkillImproverModule
    ) -> None:
        ctx = MagicMock()
        await module._on_post_respond(ctx)  # must not raise

    @pytest.mark.asyncio
    async def test_on_post_respond_spawns_optimization_when_threshold_met(
        self, module: SkillImproverModule
    ) -> None:
        """When usage count >= optimize_after_uses, spawn_background is called."""
        collector = MagicMock()
        # Usage count at threshold
        collector.usage_counts = {"my-skill": module._config.optimize_after_uses}
        collector.reset_count = MagicMock()
        module._collector = collector

        ctx = MagicMock()
        with patch(
            "arcagent.modules.skill_improver.skill_improver_module.spawn_background"
        ) as mock_spawn:
            await module._on_post_respond(ctx)

        mock_spawn.assert_called_once()
        collector.reset_count.assert_called_once_with("my-skill")

    @pytest.mark.asyncio
    async def test_on_ready_sets_collector_and_registry(
        self, module: SkillImproverModule, workspace: Path
    ) -> None:
        """_on_ready with skill_registry creates TraceCollector and sets _skill_registry."""
        mock_registry = MagicMock()
        ctx = MagicMock()
        ctx.data = {"skill_registry": mock_registry}

        with patch(
            "arcagent.modules.skill_improver.trace_collector.TraceCollector"
        ) as mock_collector_cls:
            mock_collector_cls.return_value = MagicMock()
            await module._on_ready(ctx)

        assert module._skill_registry is mock_registry
        assert module._collector is not None
        mock_collector_cls.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_ready_noop_when_no_registry(
        self, module: SkillImproverModule
    ) -> None:
        """_on_ready with no skill_registry warns and disables collection."""
        ctx = MagicMock()
        ctx.data = {}  # no skill_registry key

        await module._on_ready(ctx)  # must not raise
        assert module._collector is None


# ---------------------------------------------------------------------------
# Shutdown with background tasks
# ---------------------------------------------------------------------------


class TestShutdownWithTasks:
    @pytest.mark.asyncio
    async def test_shutdown_awaits_background_tasks(
        self, workspace: Path, telemetry: MagicMock
    ) -> None:
        """Shutdown with in-flight tasks calls asyncio.gather on them."""
        module = SkillImproverModule(workspace=workspace, telemetry=telemetry)

        # Inject a completed task to simulate background work
        import asyncio

        async def _done() -> None:
            pass

        task = asyncio.ensure_future(_done())
        module._background_tasks.add(task)

        await module.shutdown()  # must not raise or hang
