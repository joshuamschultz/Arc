"""ImproverStateReader — the write-free read model of one skill's improver (alpha-2 P8).

Answers the operator's questions about a skill without a running agent: is it
active or retired, which candidates exist and how did they score, how many
usage traces back it, what did the eval gate decide last time and why, and how
big is its golden suite.

It reads the same files the improver writes — the candidate manifest
(:class:`~arcskill.improver.candidate_store.CandidateStore` layout), the trace
index (:class:`~arcskill.improver.trace_store.TraceStore` layout), the gate log
(:class:`~arcskill.improver.gate_log.GateLog`) and the golden suite
(:func:`~arcskill.improver.evalgate.load_suite`). A read never creates a file or a
directory, and a skill name that could escape the workspace is refused.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from arcskill.improver.candidate_store import _validate_skill_name
from arcskill.improver.evalgate import load_suite
from arcskill.improver.gate_log import GateLog

_DEFAULT_LIMIT = 20


@dataclass(frozen=True)
class CandidateSummary:
    """One candidate version: lineage + judge scores (never the body)."""

    candidate_id: str
    generation: int
    parent_id: str | None
    scores: dict[str, float]
    active: bool
    in_frontier: bool


@dataclass(frozen=True)
class TraceSummary:
    """Usage evidence the improver learns from."""

    total: int = 0
    success: int = 0
    failure: int = 0


@dataclass(frozen=True)
class SuiteSummary:
    """Golden-suite size by provenance."""

    total: int = 0
    human: int = 0
    machine: int = 0
    curated: int = 0


@dataclass(frozen=True)
class ImproverState:
    """Everything the Improver card shows for one skill."""

    skill_name: str
    lifecycle_state: str
    active_candidate_id: str | None
    generation: int
    lifecycle_reason: str
    merged_into: str | None
    traces: TraceSummary
    suite: SuiteSummary
    candidates: list[CandidateSummary] = field(default_factory=list)
    gate_log: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ImproverStateReader:
    """Read one skill's improver state from an agent workspace (no writes)."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    def read(
        self, skill_name: str, *, skill_dir: Path | None = None, limit: int = _DEFAULT_LIMIT
    ) -> ImproverState:
        """The skill's state; ``skill_dir`` (its bundle) adds the golden-suite summary."""
        _validate_skill_name(skill_name)
        base = self._workspace / "skill_traces" / skill_name
        manifest = _read_json(base / "candidates" / "manifest.json")
        active_id = _optional_str(manifest.get("active_candidate_id"))
        lifecycle = str(manifest.get("lifecycle_state", "active"))
        return ImproverState(
            skill_name=skill_name,
            lifecycle_state=lifecycle,
            active_candidate_id=active_id,
            generation=_int(manifest.get("generation")),
            lifecycle_reason=str(
                manifest.get("retire_reason") or manifest.get("merged_reason") or ""
            ),
            merged_into=_optional_str(manifest.get("merged_into")),
            traces=_trace_summary(base / "index.json"),
            suite=_suite_summary(skill_dir),
            candidates=_candidates(manifest, active_id, limit),
            gate_log=[
                r.to_dict() for r in GateLog(self._workspace).recent(skill_name, limit=limit)
            ],
        )


def _candidates(
    manifest: dict[str, Any], active_id: str | None, limit: int
) -> list[CandidateSummary]:
    """Newest generation first; the manifest keeps metadata only (bodies stay on disk)."""
    raw = manifest.get("candidates")
    frontier = set(manifest.get("frontier") or [])
    if not isinstance(raw, dict):
        return []
    rows = [
        CandidateSummary(
            candidate_id=str(cid),
            generation=_int(meta.get("generation")),
            parent_id=_optional_str(meta.get("parent_id")),
            scores=_scores(meta.get("scores")),
            active=cid == active_id,
            in_frontier=cid in frontier,
        )
        for cid, meta in raw.items()
        if isinstance(meta, dict)
    ]
    rows.sort(key=lambda row: row.generation, reverse=True)
    return rows[:limit]


def _trace_summary(index_path: Path) -> TraceSummary:
    index = _read_json(index_path)
    return TraceSummary(
        total=_int(index.get("total_traces")),
        success=_int(index.get("success_count")),
        failure=_int(index.get("failure_count")),
    )


def _suite_summary(skill_dir: Path | None) -> SuiteSummary:
    cases = load_suite(skill_dir)
    curated = sum(1 for c in cases if c.curated)
    machine = sum(1 for c in cases if c.machine_authored)
    return SuiteSummary(
        total=len(cases), human=len(cases) - curated - machine, machine=machine, curated=curated
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _scores(raw: object) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    return {str(k): float(v) for k, v in raw.items() if isinstance(v, int | float)}


def _int(raw: object) -> int:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else 0


def _optional_str(raw: object) -> str | None:
    return raw if isinstance(raw, str) and raw else None


__all__ = [
    "CandidateSummary",
    "ImproverState",
    "ImproverStateReader",
    "SuiteSummary",
    "TraceSummary",
]
