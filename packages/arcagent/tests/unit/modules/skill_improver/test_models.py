"""Tests for skill_improver models — serialization, deserialization, fingerprinting."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from arcagent.modules.skill_improver.models import (
    Candidate,
    DimensionScore,
    EvalResult,
    MutationEvent,
    SkillTrace,
    ToolCallRecord,
    _text_fingerprint,
)


class TestToolCallRecord:
    """ToolCallRecord round-trip serialization."""

    def test_to_dict_and_back(self) -> None:
        tc = ToolCallRecord(
            tool_name="read",
            args_hash="abc123",
            result_status="ok",
            duration_ms=42.5,
            error_type=None,
        )
        d = tc.to_dict()
        restored = ToolCallRecord.from_dict(d)
        assert restored == tc

    def test_to_dict_with_error(self) -> None:
        tc = ToolCallRecord(
            tool_name="bash",
            args_hash="def456",
            result_status="error",
            duration_ms=100.0,
            error_type="PermissionError",
        )
        d = tc.to_dict()
        assert d["error_type"] == "PermissionError"
        restored = ToolCallRecord.from_dict(d)
        assert restored.error_type == "PermissionError"

    def test_frozen(self) -> None:
        tc = ToolCallRecord(
            tool_name="read",
            args_hash="x",
            result_status="ok",
            duration_ms=1.0,
        )
        try:
            tc.tool_name = "write"  # type: ignore[misc]
            raise AssertionError("Should be frozen")
        except AttributeError:
            pass


class TestSkillTrace:
    """SkillTrace round-trip serialization."""

    def _make_trace(self) -> SkillTrace:
        return SkillTrace(
            trace_id="trace-001",
            session_id="session-001",
            skill_name="plan-travel",
            skill_version=0,
            turn_number=5,
            started_at=datetime(2026, 2, 25, 10, 0, 0, tzinfo=UTC),
            ended_at=datetime(2026, 2, 25, 10, 1, 0, tzinfo=UTC),
            tool_calls=[
                ToolCallRecord(
                    tool_name="read",
                    args_hash="hash1",
                    result_status="ok",
                    duration_ms=10.0,
                ),
                ToolCallRecord(
                    tool_name="bash",
                    args_hash="hash2",
                    result_status="error",
                    duration_ms=50.0,
                    error_type="TimeoutError",
                ),
            ],
            expected_tools=["read", "bash", "write"],
            coverage_pct=66.7,
            task_summary="Plan a business trip to Tokyo",
            task_outcome="partial",
            outcome_source="heuristic",
        )

    def test_round_trip(self) -> None:
        trace = self._make_trace()
        d = trace.to_dict()
        restored = SkillTrace.from_dict(d)
        assert restored.trace_id == trace.trace_id
        assert restored.skill_name == trace.skill_name
        assert len(restored.tool_calls) == 2
        assert restored.tool_calls[1].error_type == "TimeoutError"
        assert restored.coverage_pct == 66.7
        assert restored.task_outcome == "partial"

    def test_json_serializable(self) -> None:
        trace = self._make_trace()
        line = json.dumps(trace.to_dict())
        restored_dict = json.loads(line)
        restored = SkillTrace.from_dict(restored_dict)
        assert restored.trace_id == "trace-001"

    def test_from_dict_with_missing_optional_fields(self) -> None:
        minimal = {
            "trace_id": "t1",
            "session_id": "s1",
            "skill_name": "test",
            "skill_version": 0,
            "turn_number": 1,
            "started_at": "2026-02-25T10:00:00+00:00",
        }
        trace = SkillTrace.from_dict(minimal)
        assert trace.ended_at is None
        assert trace.tool_calls == []
        assert trace.task_outcome is None


class TestDimensionScore:
    """DimensionScore round-trip."""

    def test_round_trip(self) -> None:
        ds = DimensionScore(
            dimension="accuracy",
            score=4,
            checklist_results=[
                {"item": "Steps correct", "answer": True, "reason": "all good"},
            ],
            rationale="Solid procedure",
        )
        d = ds.to_dict()
        restored = DimensionScore.from_dict(d)
        assert restored.dimension == "accuracy"
        assert restored.score == 4

    def test_frozen(self) -> None:
        ds = DimensionScore(dimension="clarity", score=3)
        try:
            ds.score = 5  # type: ignore[misc]
            raise AssertionError("Should be frozen")
        except AttributeError:
            pass


class TestEvalResult:
    """EvalResult aggregate computation."""

    def test_compute_aggregates_empty(self) -> None:
        er = EvalResult(per_trace_scores=[])
        er.compute_aggregates()
        assert er.aggregate_scores == {}

    def test_compute_aggregates_single_trace(self) -> None:
        er = EvalResult(
            per_trace_scores=[
                {
                    "accuracy": DimensionScore(dimension="accuracy", score=4),
                    "clarity": DimensionScore(dimension="clarity", score=3),
                }
            ]
        )
        er.compute_aggregates()
        assert er.aggregate_scores["accuracy"] == 4.0
        assert er.aggregate_scores["clarity"] == 3.0

    def test_compute_aggregates_multiple_traces(self) -> None:
        er = EvalResult(
            per_trace_scores=[
                {
                    "accuracy": DimensionScore(dimension="accuracy", score=4),
                    "clarity": DimensionScore(dimension="clarity", score=2),
                },
                {
                    "accuracy": DimensionScore(dimension="accuracy", score=2),
                    "clarity": DimensionScore(dimension="clarity", score=4),
                },
            ]
        )
        er.compute_aggregates()
        assert er.aggregate_scores["accuracy"] == 3.0
        assert er.aggregate_scores["clarity"] == 3.0


class TestCandidate:
    """Candidate fingerprinting and serialization."""

    def test_auto_fingerprint(self) -> None:
        c = Candidate(id="c1", text="Some skill text")
        assert c.fingerprint == _text_fingerprint("Some skill text")
        assert len(c.fingerprint) == 64  # SHA-256 hex

    def test_explicit_fingerprint_preserved(self) -> None:
        c = Candidate(id="c1", text="text", fingerprint="custom")
        assert c.fingerprint == "custom"

    def test_round_trip(self) -> None:
        c = Candidate(
            id="c1",
            text="# Skill\nDo the thing",
            scores={"accuracy": [4.0, 3.0]},
            aggregate_scores={"accuracy": 3.5},
            token_count=10,
            parent_id="seed",
            generation=1,
            created_at=datetime(2026, 2, 25, 12, 0, 0, tzinfo=UTC),
        )
        d = c.to_dict()
        restored = Candidate.from_dict(d)
        assert restored.id == "c1"
        assert restored.parent_id == "seed"
        assert restored.generation == 1
        assert restored.token_count == 10

    def test_different_text_different_fingerprint(self) -> None:
        c1 = Candidate(id="a", text="text A")
        c2 = Candidate(id="b", text="text B")
        assert c1.fingerprint != c2.fingerprint


class TestMutationEvent:
    """MutationEvent audit serialization."""

    def _make_event(self) -> MutationEvent:
        return MutationEvent(
            timestamp=datetime(2026, 2, 25, 14, 30, 0, tzinfo=UTC),
            skill_name="plan-travel",
            previous_hash="aaa",
            new_hash="bbb",
            candidate_id="c2",
            generation=1,
            scores={"accuracy": 4.0, "clarity": 3.5},
            improvement={"accuracy": 0.5, "clarity": 0.2},
            stop_reason="stagnation",
            trace_ids=["t1", "t2", "t3"],
        )

    def test_round_trip(self) -> None:
        event = self._make_event()
        d = event.to_dict()
        restored = MutationEvent.from_dict(d)
        assert restored.skill_name == "plan-travel"
        assert restored.generation == 1
        assert len(restored.trace_ids) == 3

    def test_to_json_line(self) -> None:
        event = self._make_event()
        line = event.to_json_line()
        parsed = json.loads(line)
        assert parsed["skill_name"] == "plan-travel"
        assert parsed["stop_reason"] == "stagnation"

    def test_frozen(self) -> None:
        event = self._make_event()
        try:
            event.skill_name = "other"  # type: ignore[misc]
            raise AssertionError("Should be frozen")
        except AttributeError:
            pass


class TestTextFingerprint:
    """_text_fingerprint utility."""

    def test_deterministic(self) -> None:
        assert _text_fingerprint("hello") == _text_fingerprint("hello")

    def test_different_inputs(self) -> None:
        assert _text_fingerprint("hello") != _text_fingerprint("world")

    def test_sha256_length(self) -> None:
        assert len(_text_fingerprint("test")) == 64
