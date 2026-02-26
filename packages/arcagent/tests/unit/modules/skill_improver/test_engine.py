"""Tests for SkillOptimizer — trace splitting, optimization loop, application."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from arcagent.modules.skill_improver.candidate_store import CandidateStore
from arcagent.modules.skill_improver.config import SkillImproverConfig
from arcagent.modules.skill_improver.engine import SkillOptimizer
from arcagent.modules.skill_improver.evaluator import SkillEvaluator
from arcagent.modules.skill_improver.guardrails import Guardrails
from arcagent.modules.skill_improver.models import (
    Candidate,
    DimensionScore,
    EvalResult,
    SkillTrace,
    ToolCallRecord,
)
from arcagent.modules.skill_improver.reflector import SkillReflector


def _make_trace(turn: int = 0, outcome: str = "success") -> SkillTrace:
    return SkillTrace(
        trace_id=f"trace-{turn}",
        session_id="s1",
        skill_name="test-skill",
        skill_version=0,
        turn_number=turn,
        started_at=datetime(2026, 2, 25, 10, 0, 0, tzinfo=UTC),
        ended_at=datetime(2026, 2, 25, 10, 1, 0, tzinfo=UTC),
        tool_calls=[
            ToolCallRecord(
                tool_name="read",
                args_hash="h",
                result_status="ok",
                duration_ms=10.0,
            ),
        ],
        task_summary="Test task",
        task_outcome=outcome,
    )


SKILL_TEXT = """\
## SKILL INTENT [IMMUTABLE]
Test skill.

## Steps
1. Do something
2. Do another thing
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def config() -> SkillImproverConfig:
    return SkillImproverConfig(
        min_traces=5,
        max_iterations=3,
        stagnation_limit=2,
        eval_dimensions=["accuracy"],
    )


@pytest.fixture
def mock_evaluator() -> AsyncMock:
    evaluator = AsyncMock(spec=SkillEvaluator)
    # Return a result with scores
    result = EvalResult(
        per_trace_scores=[
            {"accuracy": DimensionScore(dimension="accuracy", score=3)},
        ],
    )
    result.compute_aggregates()
    evaluator.evaluate.return_value = result
    return evaluator


@pytest.fixture
def mock_reflector() -> AsyncMock:
    reflector = AsyncMock(spec=SkillReflector)
    reflector.reflect.return_value = (
        "## SKILL INTENT [IMMUTABLE]\nTest skill.\n\n## Steps\n1. Improved step\n2. Better step\n"
    )
    return reflector


@pytest.fixture
def guardrails(config: SkillImproverConfig) -> Guardrails:
    return Guardrails(config)


@pytest.fixture
def store(workspace: Path) -> CandidateStore:
    return CandidateStore(workspace)


@pytest.fixture
def optimizer(
    config: SkillImproverConfig,
    mock_evaluator: AsyncMock,
    mock_reflector: AsyncMock,
    guardrails: Guardrails,
    store: CandidateStore,
) -> SkillOptimizer:
    return SkillOptimizer(
        config=config,
        evaluator=mock_evaluator,
        reflector=mock_reflector,
        guardrails=guardrails,
        store=store,
    )


class TestTraceSplitting:
    """G1: 70/30 train/holdout, deterministic seed."""

    def test_split_ratio(self, optimizer: SkillOptimizer) -> None:
        traces = [_make_trace(turn=i) for i in range(10)]
        train, holdout = optimizer.split_traces(traces, ratio=0.7)
        assert len(train) == 7
        assert len(holdout) == 3

    def test_split_deterministic(self, optimizer: SkillOptimizer) -> None:
        traces = [_make_trace(turn=i) for i in range(10)]
        t1, _h1 = optimizer.split_traces(traces, ratio=0.7, seed=42)
        t2, _h2 = optimizer.split_traces(traces, ratio=0.7, seed=42)
        assert [t.trace_id for t in t1] == [t.trace_id for t in t2]

    def test_split_minimum_holdout(self, optimizer: SkillOptimizer) -> None:
        traces = [_make_trace(turn=i) for i in range(3)]
        _train, holdout = optimizer.split_traces(traces, ratio=0.7)
        assert len(holdout) >= 1


class TestMinibatchSampling:
    """G2: Random subset of train traces."""

    def test_minibatch_size(self, optimizer: SkillOptimizer) -> None:
        traces = [_make_trace(turn=i) for i in range(20)]
        batch = optimizer.sample_minibatch(traces, size=5)
        assert len(batch) == 5

    def test_minibatch_full_if_smaller(self, optimizer: SkillOptimizer) -> None:
        traces = [_make_trace(turn=i) for i in range(3)]
        batch = optimizer.sample_minibatch(traces, size=10)
        assert len(batch) == 3


class TestStagnationDetection:
    """G4: Stop after N gens with no frontier change."""

    @pytest.mark.asyncio
    async def test_stops_on_stagnation(
        self,
        optimizer: SkillOptimizer,
        mock_reflector: AsyncMock,
    ) -> None:
        # Reflector returns same text each time (no improvement)
        mock_reflector.reflect.return_value = SKILL_TEXT
        traces = [_make_trace(turn=i) for i in range(10)]
        result = await optimizer.optimize("test-skill", SKILL_TEXT, traces)
        assert result is not None
        assert result.stop_reason in ("stagnation", "max_iterations")


class TestOptimizationLoop:
    """G5: Multiple iterations, returns OptimizeResult."""

    @pytest.mark.asyncio
    async def test_returns_optimize_result(
        self,
        optimizer: SkillOptimizer,
        mock_evaluator: AsyncMock,
        mock_reflector: AsyncMock,
    ) -> None:
        # Use a callable side_effect that alternates between low and improved scores
        call_count = 0

        async def _eval_side_effect(*args: Any, **kwargs: Any) -> EvalResult:
            nonlocal call_count
            call_count += 1
            # First call is seed eval (low score), subsequent alternate
            if call_count == 1:
                r = EvalResult(
                    per_trace_scores=[{"accuracy": DimensionScore(dimension="accuracy", score=2)}],
                )
            elif call_count % 2 == 0:
                r = EvalResult(
                    per_trace_scores=[{"accuracy": DimensionScore(dimension="accuracy", score=2)}],
                )
            else:
                r = EvalResult(
                    per_trace_scores=[{"accuracy": DimensionScore(dimension="accuracy", score=4)}],
                )
            r.compute_aggregates()
            return r

        mock_evaluator.evaluate.side_effect = _eval_side_effect

        traces = [_make_trace(turn=i) for i in range(10)]
        result = await optimizer.optimize("test-skill", SKILL_TEXT, traces)
        assert result is not None
        assert result.skill_name == "test-skill"
        assert result.iterations_run >= 1
        assert result.best_candidate is not None


class TestPostOptimizationApplication:
    """G6: Atomic write, registry rescan, audit log."""

    @pytest.mark.asyncio
    async def test_apply_writes_candidate(
        self,
        optimizer: SkillOptimizer,
        store: CandidateStore,
        workspace: Path,
    ) -> None:
        candidate = Candidate(
            id="c1",
            text="# Improved\nBetter steps",
            aggregate_scores={"accuracy": 4.0},
            token_count=5,
            generation=1,
        )
        skill_path = workspace / "skills" / "test-skill.md"
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(SKILL_TEXT)

        optimizer.apply_result(
            "test-skill",
            candidate,
            skill_path=skill_path,
            seed_scores={"accuracy": 2.0},
            trace_ids=["t1"],
        )

        # Verify file updated
        assert "Improved" in skill_path.read_text()
        # Verify audit log exists
        audit_path = store._skill_dir("test-skill") / "audit.jsonl"
        assert audit_path.exists()
