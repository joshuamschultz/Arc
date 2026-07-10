"""SPEC-054 COMP-007 — trace promoter (REQ-118).

Pins ``arcskill.improver.promote``:

* ``promote_traces(traces, *, skill_dir) -> PromotionResult(anchors, repros, skipped)``
* ``canonicalize(text) -> str`` — ISO-8601 timestamps → ``<TIMESTAMP>``; uuid4
  (hyphenated or 32-hex) → ``<UUID>``
* ``retire_stale(skill_dir, current_version) -> list[str]`` (retired nodeids)

Evaluator-labeled successes become deterministic replay anchors at
``evals/promoted/test_promoted_<trace_id[:8]>.py`` (single function
``test_replay_<trace_id[:8]>``, ``@generated`` module docstring carrying
``skill_version=<N>``, manifest entry ``promoted/<file>`` with sha256 + skill_version)
so :func:`~arcskill.improver.evalgate.load_suite` classifies them machine-authored and
they enter the same adoption-cascade handoff as suitegen anchors. Observed failures
become quarantine-side repro files ``evals/promoted/repro_<trace_id[:8]>.py`` plus a
manifest ``quarantine`` entry (reason: improvement target) — the ``repro_`` prefix keeps
them structurally invisible to load_suite, never adopted anchors. Heuristic-only
outcomes are not promotable. ``retire_stale`` retires version-mismatched anchors
VISIBLY: returns their nodeids, deletes the files, and moves their manifest entries to
a ``retired`` list — never a silent skip.
"""

from __future__ import annotations

import ast
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arcskill.improver.evalgate import load_suite
from arcskill.improver.models import EvalCase, SkillTrace, ToolCallRecord
from arcskill.improver.promote import (
    PromotionResult,
    canonicalize,
    promote_traces,
    retire_stale,
)
from arcskill.improver.suitegen import QuarantinedCase

_TRACE_ID = "aabbccdd-1122-4333-8444-555566667777"  # id8 = aabbccdd
_OTHER_ID = "11223344-5566-4777-8899-aabbccddeeff"  # id8 = 11223344
_VOLATILE_TS = "2026-07-10T09:15:00+00:00"
_VOLATILE_UUID = "9f1c2d3e-4a5b-4c6d-8e7f-0a1b2c3d4e5f"
_STARTED = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


def _skill_dir(tmp_path: Path) -> Path:
    skill_dir = tmp_path / "calc"
    (skill_dir / "evals").mkdir(parents=True)
    return skill_dir


def _trace(
    *,
    trace_id: str = _TRACE_ID,
    outcome: str,
    source: str,
    version: int = 3,
    args: dict[str, Any] | None = None,
    status: str = "ok",
) -> SkillTrace:
    return SkillTrace(
        trace_id=trace_id,
        session_id="sess-1",
        skill_name="calc",
        skill_version=version,
        turn_number=0,
        started_at=_STARTED,
        ended_at=_STARTED,
        tool_calls=[
            ToolCallRecord(
                tool_name="fetch_data",
                args_hash="",
                result_status=status,
                duration_ms=1.0,
                error_type="ToolError" if status == "error" else None,
                args=args,
            )
        ],
        task_outcome=outcome,
        outcome_source=source,
    )


def _manifest(skill_dir: Path) -> dict[str, Any]:
    raw: dict[str, Any] = json.loads(
        (skill_dir / "evals" / ".manifest.json").read_text(encoding="utf-8")
    )
    return raw


# -- evaluator success → deterministic replay anchor (REQ-118) ---------------------


def test_evaluator_success_promotes_deterministic_replay_anchor(tmp_path: Path) -> None:
    skill_dir = _skill_dir(tmp_path)
    args = {"since": _VOLATILE_TS, "request_id": _VOLATILE_UUID, "city": "tokyo"}

    result = promote_traces(
        [_trace(outcome="success", source="evaluator", args=args)], skill_dir=skill_dir
    )

    assert isinstance(result, PromotionResult)
    assert result.repros == []
    assert len(result.anchors) == 1
    case = result.anchors[0]
    assert isinstance(case, EvalCase)
    assert case.machine_authored is True
    assert case.node == "evals/promoted/test_promoted_aabbccdd.py::test_replay_aabbccdd"

    path = skill_dir / "evals" / "promoted" / "test_promoted_aabbccdd.py"
    text = path.read_text(encoding="utf-8")
    docstring = ast.get_docstring(ast.parse(text)) or ""
    assert "@generated" in docstring
    assert "skill_version=3" in docstring  # pinned to the trace's skill_version

    # volatile fields canonicalized; non-volatile recorded data survives
    assert _VOLATILE_TS not in text
    assert _VOLATILE_UUID not in text
    assert "<TIMESTAMP>" in text
    assert "<UUID>" in text
    assert "tokyo" in text

    entry = _manifest(skill_dir)["files"]["promoted/test_promoted_aabbccdd.py"]
    assert entry["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert entry["skill_version"] == 3

    # COMP-002 handoff: load_suite sees the promoted case with machine provenance
    cases = load_suite(skill_dir)
    assert [c.node for c in cases] == [case.node]
    assert cases[0].machine_authored is True


def test_promotion_is_deterministic_across_runs(tmp_path: Path) -> None:
    trace = _trace(outcome="success", source="evaluator", args={"since": _VOLATILE_TS})
    contents: list[bytes] = []
    for name in ("one", "two"):
        skill_dir = tmp_path / name / "calc"
        (skill_dir / "evals").mkdir(parents=True)
        promote_traces([trace], skill_dir=skill_dir)
        generated = skill_dir / "evals" / "promoted" / "test_promoted_aabbccdd.py"
        contents.append(generated.read_bytes())
    assert contents[0] == contents[1]


def test_canonicalize_replaces_iso_timestamps_and_uuid4() -> None:
    assert canonicalize("at 2026-07-10T09:15:00+00:00 sharp") == "at <TIMESTAMP> sharp"
    assert canonicalize("t=2026-07-10T09:15:00.123456+00:00") == "t=<TIMESTAMP>"
    assert canonicalize("t=2026-07-10T09:15:00Z") == "t=<TIMESTAMP>"
    assert canonicalize(f"id {_VOLATILE_UUID}") == "id <UUID>"
    assert canonicalize(f"hex {_VOLATILE_UUID.replace('-', '')}") == "hex <UUID>"
    assert canonicalize("plain prose stays") == "plain prose stays"


# -- observed failure → quarantine-side repro, never an anchor (REQ-118) -----------


def test_observed_failure_promotes_quarantine_side_repro(tmp_path: Path) -> None:
    skill_dir = _skill_dir(tmp_path)
    trace = _trace(
        outcome="failure", source="heuristic", status="error", version=4, args={"city": "tokyo"}
    )

    result = promote_traces([trace], skill_dir=skill_dir)

    assert result.anchors == []
    assert len(result.repros) == 1
    repro = result.repros[0]
    assert isinstance(repro, QuarantinedCase)
    assert "improvement" in repro.reason.lower()
    assert repro.nodeid == "evals/promoted/repro_aabbccdd.py"

    assert (skill_dir / "evals" / "promoted" / "repro_aabbccdd.py").exists()
    # quarantine-side: NOT an adopted anchor — invisible to load_suite
    assert load_suite(skill_dir) == []

    quarantine = _manifest(skill_dir)["quarantine"]
    assert len(quarantine) == 1
    assert quarantine[0]["nodeid"] == repro.nodeid
    assert "improvement" in quarantine[0]["reason"].lower()
    assert quarantine[0]["skill_version"] == 4


# -- heuristic-only outcomes are not promotable (REQ-118) --------------------------


def test_heuristic_only_success_is_not_promotable(tmp_path: Path) -> None:
    skill_dir = _skill_dir(tmp_path)

    result = promote_traces([_trace(outcome="success", source="heuristic")], skill_dir=skill_dir)

    assert result.anchors == []
    assert result.repros == []
    assert result.skipped == 1
    # skipped means skipped: no files, no manifest side effects
    assert not (skill_dir / "evals" / "promoted").exists()
    assert not (skill_dir / "evals" / ".manifest.json").exists()


# -- expiry: version-mismatched anchors retired visibly (REQ-118) -------------------


def test_retire_stale_visibly_retires_version_mismatched_anchors(tmp_path: Path) -> None:
    skill_dir = _skill_dir(tmp_path)
    # pre-existing suitegen-style manifest entry (no skill_version) must be ignored
    golden_entry = {"sha256": "0" * 64}
    (skill_dir / "evals" / ".manifest.json").write_text(
        json.dumps({"files": {"test_golden_generated.py": golden_entry}}), encoding="utf-8"
    )
    stale = _trace(trace_id=_TRACE_ID, outcome="success", source="evaluator", version=3)
    fresh = _trace(trace_id=_OTHER_ID, outcome="success", source="evaluator", version=5)
    promote_traces([stale, fresh], skill_dir=skill_dir)

    retired = retire_stale(skill_dir, current_version=5)

    stale_nodeid = "evals/promoted/test_promoted_aabbccdd.py::test_replay_aabbccdd"
    assert retired == [stale_nodeid]
    assert not (skill_dir / "evals" / "promoted" / "test_promoted_aabbccdd.py").exists()

    manifest = _manifest(skill_dir)
    assert "promoted/test_promoted_aabbccdd.py" not in manifest["files"]
    assert manifest["files"]["test_golden_generated.py"] == golden_entry  # untouched
    retired_entries = manifest["retired"]
    assert len(retired_entries) == 1
    assert retired_entries[0]["nodeid"] == stale_nodeid
    assert retired_entries[0]["skill_version"] == 3

    # the current-version anchor survives and still loads
    assert [c.node for c in load_suite(skill_dir)] == [
        "evals/promoted/test_promoted_11223344.py::test_replay_11223344"
    ]
    # idempotent: nothing stale remains
    assert retire_stale(skill_dir, current_version=5) == []


def test_retire_stale_with_no_promoted_cases_returns_empty(tmp_path: Path) -> None:
    assert retire_stale(_skill_dir(tmp_path), current_version=1) == []
