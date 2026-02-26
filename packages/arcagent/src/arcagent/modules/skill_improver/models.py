"""Data models for the skill_improver module.

Immutable trace records, candidate versions, evaluation results,
and audit events. All serializable to JSON for JSONL storage.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class ToolCallRecord:
    """Single tool invocation within a skill execution span."""

    tool_name: str
    args_hash: str  # SHA-256 of sanitized args
    result_status: str  # "ok" | "error" | "vetoed"
    duration_ms: float
    error_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "args_hash": self.args_hash,
            "result_status": self.result_status,
            "duration_ms": self.duration_ms,
            "error_type": self.error_type,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolCallRecord:
        return cls(
            tool_name=str(data["tool_name"]),
            args_hash=str(data["args_hash"]),
            result_status=str(data["result_status"]),
            duration_ms=float(data["duration_ms"]),
            error_type=str(data["error_type"]) if data.get("error_type") else None,
        )


@dataclass
class SkillTrace:
    """Execution trace for a single skill usage span."""

    trace_id: str
    session_id: str
    skill_name: str
    skill_version: int  # 0 = seed
    turn_number: int
    started_at: datetime
    ended_at: datetime | None = None
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    expected_tools: list[str] = field(default_factory=list)
    coverage_pct: float = 0.0
    task_summary: str = ""
    task_outcome: str | None = None  # "success" | "failure" | "partial"
    outcome_source: str | None = None  # "heuristic" | "evaluator"

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "skill_name": self.skill_name,
            "skill_version": self.skill_version,
            "turn_number": self.turn_number,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "tool_calls": [tc.to_dict() for tc in self.tool_calls],
            "expected_tools": self.expected_tools,
            "coverage_pct": self.coverage_pct,
            "task_summary": self.task_summary,
            "task_outcome": self.task_outcome,
            "outcome_source": self.outcome_source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkillTrace:
        tool_calls_raw: list[dict[str, Any]] = data.get("tool_calls", [])
        tool_calls = [ToolCallRecord.from_dict(tc) for tc in tool_calls_raw]
        started_at_str = str(data["started_at"])
        ended_at_val = data.get("ended_at")
        return cls(
            trace_id=str(data["trace_id"]),
            session_id=str(data["session_id"]),
            skill_name=str(data["skill_name"]),
            skill_version=int(data["skill_version"]),
            turn_number=int(data["turn_number"]),
            started_at=datetime.fromisoformat(started_at_str),
            ended_at=datetime.fromisoformat(str(ended_at_val)) if ended_at_val else None,
            tool_calls=tool_calls,
            expected_tools=list(data.get("expected_tools", [])),
            coverage_pct=float(data.get("coverage_pct", 0.0)),
            task_summary=str(data.get("task_summary", "")),
            task_outcome=str(data["task_outcome"]) if data.get("task_outcome") else None,
            outcome_source=str(data["outcome_source"]) if data.get("outcome_source") else None,
        )


@dataclass(frozen=True)
class DimensionScore:
    """Evaluation score for a single dimension on a single trace."""

    dimension: str
    score: int  # 1-5
    checklist_results: list[dict[str, Any]] = field(default_factory=list)
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "score": self.score,
            "checklist_results": self.checklist_results,
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DimensionScore:
        return cls(
            dimension=str(data["dimension"]),
            score=int(data["score"]),
            checklist_results=list(data.get("checklist_results", [])),
            rationale=str(data.get("rationale", "")),
        )


@dataclass
class EvalResult:
    """Evaluation result across all dimensions and traces."""

    per_trace_scores: list[dict[str, DimensionScore]]
    aggregate_scores: dict[str, float] = field(default_factory=dict)

    def compute_aggregates(self) -> None:
        """Compute mean score per dimension across traces."""
        if not self.per_trace_scores:
            return
        dim_totals: dict[str, list[float]] = {}
        for trace_scores in self.per_trace_scores:
            for dim, ds in trace_scores.items():
                dim_totals.setdefault(dim, []).append(float(ds.score))
        self.aggregate_scores = {
            dim: sum(scores) / len(scores) for dim, scores in dim_totals.items()
        }


def _text_fingerprint(text: str) -> str:
    """SHA-256 fingerprint of text for quick comparison."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Candidate:
    """A versioned candidate of a skill's body text."""

    id: str  # UUID
    text: str
    scores: dict[str, list[float]] = field(default_factory=dict)  # dim -> per-trace
    aggregate_scores: dict[str, float] = field(default_factory=dict)
    token_count: int = 0
    parent_id: str | None = None
    generation: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.fingerprint:
            self.fingerprint = _text_fingerprint(self.text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "scores": self.scores,
            "aggregate_scores": self.aggregate_scores,
            "token_count": self.token_count,
            "parent_id": self.parent_id,
            "generation": self.generation,
            "created_at": self.created_at.isoformat(),
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Candidate:
        scores_raw: dict[str, list[float]] = data.get("scores", {})
        agg_raw: dict[str, float] = data.get("aggregate_scores", {})
        return cls(
            id=str(data["id"]),
            text=str(data["text"]),
            scores=scores_raw,
            aggregate_scores=agg_raw,
            token_count=int(data.get("token_count", 0)),
            parent_id=str(data["parent_id"]) if data.get("parent_id") else None,
            generation=int(data.get("generation", 0)),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            fingerprint=str(data.get("fingerprint", "")),
        )


@dataclass
class OptimizeResult:
    """Result of an optimization run."""

    skill_name: str
    best_candidate: Candidate
    frontier: list[Candidate]
    iterations_run: int
    stop_reason: str  # "stagnation" | "max_iterations" | "guardrail"
    seed_scores: dict[str, float]
    improvement: dict[str, float]  # Per-dimension delta from seed

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "best_candidate_id": self.best_candidate.id,
            "iterations_run": self.iterations_run,
            "stop_reason": self.stop_reason,
            "seed_scores": self.seed_scores,
            "improvement": self.improvement,
        }


@dataclass(frozen=True)
class MutationEvent:
    """Append-only audit record for a skill mutation (NIST AU-3)."""

    timestamp: datetime
    skill_name: str
    previous_hash: str
    new_hash: str
    candidate_id: str
    generation: int
    scores: dict[str, float]
    improvement: dict[str, float]
    stop_reason: str
    trace_ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "skill_name": self.skill_name,
            "previous_hash": self.previous_hash,
            "new_hash": self.new_hash,
            "candidate_id": self.candidate_id,
            "generation": self.generation,
            "scores": dict(self.scores),
            "improvement": dict(self.improvement),
            "stop_reason": self.stop_reason,
            "trace_ids": list(self.trace_ids),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MutationEvent:
        scores_raw: dict[str, float] = data.get("scores", {})
        improvement_raw: dict[str, float] = data.get("improvement", {})
        trace_ids_raw: list[str] = data.get("trace_ids", [])
        return cls(
            timestamp=datetime.fromisoformat(str(data["timestamp"])),
            skill_name=str(data["skill_name"]),
            previous_hash=str(data["previous_hash"]),
            new_hash=str(data["new_hash"]),
            candidate_id=str(data["candidate_id"]),
            generation=int(data["generation"]),
            scores=scores_raw,
            improvement=improvement_raw,
            stop_reason=str(data["stop_reason"]),
            trace_ids=trace_ids_raw,
        )

    def to_json_line(self) -> str:
        """Serialize to a single JSON line for append-only audit log."""
        return json.dumps(self.to_dict(), default=str)
