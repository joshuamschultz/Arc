"""Tests for SkillEvaluator — LLM-as-judge prompt construction, score parsing, evaluation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.modules.skill_improver.config import SkillImproverConfig
from arcagent.modules.skill_improver.evaluator import SkillEvaluator
from arcagent.modules.skill_improver.models import SkillTrace, ToolCallRecord


def _make_trace(
    skill_name: str = "test-skill",
    outcome: str = "success",
    coverage: float = 80.0,
) -> SkillTrace:
    return SkillTrace(
        trace_id="trace-1",
        session_id="s1",
        skill_name=skill_name,
        skill_version=0,
        turn_number=1,
        started_at=datetime(2026, 2, 25, 10, 0, 0, tzinfo=UTC),
        ended_at=datetime(2026, 2, 25, 10, 1, 0, tzinfo=UTC),
        tool_calls=[
            ToolCallRecord(
                tool_name="read",
                args_hash="h1",
                result_status="ok",
                duration_ms=10.0,
            ),
            ToolCallRecord(
                tool_name="bash",
                args_hash="h2",
                result_status="error",
                duration_ms=50.0,
                error_type="TimeoutError",
            ),
        ],
        expected_tools=["read", "bash", "write"],
        coverage_pct=coverage,
        task_summary="Plan a business trip",
        task_outcome=outcome,
        outcome_source="heuristic",
    )


SKILL_TEXT = """\
## SKILL INTENT [IMMUTABLE]
Plan business travel efficiently.

## Steps
1. Check calendar
2. Book flights
3. Confirm hotel
"""


def _make_judge_response(score: int = 3) -> str:
    """Simulate an LLM judge response."""
    return json.dumps(
        {
            "checklist": [
                {"item": "Item 1", "answer": True, "reason": "Looks good"},
                {"item": "Item 2", "answer": True, "reason": "OK"},
                {"item": "Item 3", "answer": False, "reason": "Missing"},
                {"item": "Item 4", "answer": True, "reason": "Fine"},
                {"item": "Item 5", "answer": False, "reason": "Unclear"},
            ],
            "score": score,
            "rationale": "Decent procedure with some gaps.",
        }
    )


class TestJudgePromptConstruction:
    """D1: One dimension, includes checklist + anchors + trace + anti-inflation."""

    def test_prompt_contains_dimension(self) -> None:
        config = SkillImproverConfig()
        evaluator = SkillEvaluator(config, llm=MagicMock())
        trace = _make_trace()
        prompt = evaluator.build_judge_prompt(SKILL_TEXT, trace, "accuracy")
        assert "accuracy" in prompt.lower()

    def test_prompt_contains_checklist(self) -> None:
        config = SkillImproverConfig()
        evaluator = SkillEvaluator(config, llm=MagicMock())
        trace = _make_trace()
        prompt = evaluator.build_judge_prompt(SKILL_TEXT, trace, "accuracy")
        assert "checklist" in prompt.lower() or "YES" in prompt or "NO" in prompt

    def test_prompt_contains_anti_inflation(self) -> None:
        config = SkillImproverConfig()
        evaluator = SkillEvaluator(config, llm=MagicMock())
        trace = _make_trace()
        prompt = evaluator.build_judge_prompt(SKILL_TEXT, trace, "accuracy")
        assert "2-4" in prompt or "most procedures" in prompt.lower()

    def test_prompt_contains_trace_data(self) -> None:
        config = SkillImproverConfig()
        evaluator = SkillEvaluator(config, llm=MagicMock())
        trace = _make_trace()
        prompt = evaluator.build_judge_prompt(SKILL_TEXT, trace, "accuracy")
        assert "Plan a business trip" in prompt
        assert "TimeoutError" in prompt or "error" in prompt.lower()

    def test_prompt_contains_skill_text(self) -> None:
        config = SkillImproverConfig()
        evaluator = SkillEvaluator(config, llm=MagicMock())
        trace = _make_trace()
        prompt = evaluator.build_judge_prompt(SKILL_TEXT, trace, "accuracy")
        assert "Check calendar" in prompt


class TestScoreParsing:
    """D2: JSON response -> DimensionScore with checklist results."""

    def test_parse_valid_response(self) -> None:
        config = SkillImproverConfig()
        evaluator = SkillEvaluator(config, llm=MagicMock())
        response = _make_judge_response(score=4)
        ds = evaluator.parse_score(response, "accuracy")
        assert ds.dimension == "accuracy"
        assert ds.score == 4
        assert len(ds.checklist_results) == 5

    def test_parse_fenced_json(self) -> None:
        config = SkillImproverConfig()
        evaluator = SkillEvaluator(config, llm=MagicMock())
        response = f"```json\n{_make_judge_response(score=3)}\n```"
        ds = evaluator.parse_score(response, "clarity")
        assert ds.dimension == "clarity"
        assert ds.score == 3

    def test_parse_score_clamped_to_scale(self) -> None:
        config = SkillImproverConfig(eval_scale=5)
        evaluator = SkillEvaluator(config, llm=MagicMock())
        # Score above scale
        response = json.dumps({"checklist": [], "score": 8, "rationale": "Inflated"})
        ds = evaluator.parse_score(response, "accuracy")
        assert ds.score == 5  # Clamped to max


class TestPerTraceEvaluation:
    """D3: Calls judge once per dimension per trace."""

    @pytest.mark.asyncio
    async def test_evaluate_dimension_calls_llm(self) -> None:
        config = SkillImproverConfig()
        mock_llm = AsyncMock()
        mock_llm.invoke.return_value = _make_judge_response(score=3)
        evaluator = SkillEvaluator(config, llm=mock_llm)
        trace = _make_trace()
        ds = await evaluator.evaluate_dimension(SKILL_TEXT, trace, "accuracy")
        assert ds.score == 3
        assert mock_llm.invoke.call_count == 1


class TestAggregateScores:
    """D4: Mean across traces per dimension."""

    @pytest.mark.asyncio
    async def test_evaluate_computes_aggregates(self) -> None:
        config = SkillImproverConfig(eval_dimensions=["accuracy", "clarity"])
        mock_llm = AsyncMock()
        # Return different scores per call
        mock_llm.invoke.side_effect = [
            _make_judge_response(score=4),  # trace1 accuracy
            _make_judge_response(score=2),  # trace1 clarity
            _make_judge_response(score=2),  # trace2 accuracy
            _make_judge_response(score=4),  # trace2 clarity
        ]
        evaluator = SkillEvaluator(config, llm=mock_llm)
        traces = [_make_trace(), _make_trace()]
        result = await evaluator.evaluate(SKILL_TEXT, traces)
        result.compute_aggregates()
        assert result.aggregate_scores["accuracy"] == 3.0
        assert result.aggregate_scores["clarity"] == 3.0


class TestMalformedResponse:
    """D5: Graceful fallback on malformed LLM responses."""

    def test_parse_empty_response(self) -> None:
        config = SkillImproverConfig()
        evaluator = SkillEvaluator(config, llm=MagicMock())
        ds = evaluator.parse_score("", "accuracy")
        assert ds.score == 1  # Fallback to minimum
        assert ds.dimension == "accuracy"

    def test_parse_invalid_json(self) -> None:
        config = SkillImproverConfig()
        evaluator = SkillEvaluator(config, llm=MagicMock())
        ds = evaluator.parse_score("This is not JSON at all", "clarity")
        assert ds.score == 1
        assert ds.dimension == "clarity"

    def test_parse_missing_score_field(self) -> None:
        config = SkillImproverConfig()
        evaluator = SkillEvaluator(config, llm=MagicMock())
        response = json.dumps({"checklist": [], "rationale": "No score"})
        ds = evaluator.parse_score(response, "accuracy")
        assert ds.score == 1

    @pytest.mark.asyncio
    async def test_evaluate_dimension_handles_llm_error(self) -> None:
        config = SkillImproverConfig()
        mock_llm = AsyncMock()
        mock_llm.invoke.side_effect = RuntimeError("LLM timeout")
        evaluator = SkillEvaluator(config, llm=mock_llm)
        trace = _make_trace()
        ds = await evaluator.evaluate_dimension(SKILL_TEXT, trace, "accuracy")
        assert ds.score == 1  # Fallback
