"""Trace promoter — verified runtime traces become golden-suite material (SPEC-054 REQ-118).

Evaluator-labeled successes are promoted to deterministic replay anchors at
``evals/promoted/test_promoted_<id8>.py`` (volatile timestamps/UUIDs canonicalized so the
bytes are run-independent) with an add-only manifest entry carrying the file hash and the
trace's ``skill_version`` — :func:`~arcskill.improver.evalgate.load_suite` then classifies
them machine-authored, the same handoff as suitegen anchors. Observed failures become
quarantine-side repro files ``repro_<id8>.py`` (the prefix keeps them invisible to
load_suite) plus a manifest ``quarantine`` entry — improvement targets, never anchors.
Heuristic-only outcomes carry no trusted oracle and are skipped without side effects.
:func:`retire_stale` retires version-mismatched anchors VISIBLY: file deleted, manifest
entry moved to a ``retired`` list, nodeids returned — never a silent skip.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arcskill.improver._util import atomic_write_text
from arcskill.improver.models import EvalCase, SkillTrace
from arcskill.improver.suitegen import QuarantinedCase

_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b"
    r"|\b[0-9a-fA-F]{32}\b"
)


@dataclass(frozen=True)
class PromotionResult:
    """Outcome of one promotion run: adopted anchors, quarantined repros, skips."""

    anchors: list[EvalCase]
    repros: list[QuarantinedCase]
    skipped: int


def canonicalize(text: str) -> str:
    """Replace volatile ISO-8601 timestamps with ``<TIMESTAMP>`` and uuid4 values
    (hyphenated or bare 32-hex) with ``<UUID>`` so promoted bytes are deterministic."""
    return _UUID_RE.sub("<UUID>", _TIMESTAMP_RE.sub("<TIMESTAMP>", text))


def promote_traces(traces: list[SkillTrace], *, skill_dir: Path) -> PromotionResult:
    """Promote each trace by its outcome: evaluator success → anchor, failure → repro."""
    anchors: list[EvalCase] = []
    repros: list[QuarantinedCase] = []
    skipped = 0
    for trace in traces:
        if trace.task_outcome == "failure":
            repros.append(_write_repro(trace, skill_dir))
        elif trace.task_outcome == "success" and trace.outcome_source == "evaluator":
            anchors.append(_write_anchor(trace, skill_dir))
        else:
            skipped += 1
    return PromotionResult(anchors=anchors, repros=repros, skipped=skipped)


def retire_stale(skill_dir: Path, current_version: int) -> list[str]:
    """Retire promoted anchors whose pinned ``skill_version`` mismatches *current_version*.

    Entries lacking ``skill_version`` (suitegen anchors, human files) are ignored.
    Idempotent: retired entries leave ``files``, so a second pass returns ``[]``.
    """
    evals_dir = skill_dir / "evals"
    manifest = _read_manifest(evals_dir)
    files = manifest.get("files")
    if not isinstance(files, dict):
        return []
    retired: list[str] = []
    for name in sorted(files):
        entry = files[name]
        if not isinstance(entry, dict) or "skill_version" not in entry:
            continue
        version = int(entry["skill_version"])
        if version == current_version:
            continue
        id8 = Path(name).stem.removeprefix("test_promoted_")
        nodeid = f"evals/{name}::test_replay_{id8}"
        (evals_dir / name).unlink(missing_ok=True)
        del files[name]
        manifest.setdefault("retired", []).append({"nodeid": nodeid, "skill_version": version})
        retired.append(nodeid)
    if retired:
        _write_manifest(evals_dir, manifest)
    return retired


def _write_anchor(trace: SkillTrace, skill_dir: Path) -> EvalCase:
    id8 = trace.trace_id[:8]
    filename = f"test_promoted_{id8}.py"
    content = _anchor_source(trace, id8)
    atomic_write_text(skill_dir / "evals" / "promoted" / filename, content)
    evals_dir = skill_dir / "evals"
    manifest = _read_manifest(evals_dir)
    manifest.setdefault("files", {})[f"promoted/{filename}"] = {
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "skill_version": trace.skill_version,
    }
    _write_manifest(evals_dir, manifest)
    nodeid = f"evals/promoted/{filename}::test_replay_{id8}"
    return EvalCase(id=nodeid, node=nodeid, machine_authored=True)


def _write_repro(trace: SkillTrace, skill_dir: Path) -> QuarantinedCase:
    id8 = trace.trace_id[:8]
    filename = f"repro_{id8}.py"
    atomic_write_text(skill_dir / "evals" / "promoted" / filename, _repro_source(trace, id8))
    nodeid = f"evals/promoted/{filename}"
    reason = f"improvement target: observed failure replay of trace {id8}"
    evals_dir = skill_dir / "evals"
    manifest = _read_manifest(evals_dir)
    manifest.setdefault("quarantine", []).append(
        {"nodeid": nodeid, "reason": reason, "skill_version": trace.skill_version}
    )
    _write_manifest(evals_dir, manifest)
    return QuarantinedCase(nodeid=nodeid, reason=reason)


def _anchor_source(trace: SkillTrace, id8: str) -> str:
    """Render the replay module — a pure function of the trace, so bytes are deterministic."""
    payload = canonicalize(repr(trace.to_dict()))
    return (
        '"""@generated replay anchor — machine-authored by arcskill.improver.promote.\n'
        "\n"
        f"Deterministic evaluator-verified replay (skill_version={trace.skill_version}).\n"
        '"""\n'
        "\n"
        f"TRACE = {payload}\n"
        "\n"
        "\n"
        f"def test_replay_{id8}() -> None:\n"
        '    assert TRACE["task_outcome"] == "success"\n'
        '    assert all(call["result_status"] == "ok" for call in TRACE["tool_calls"])\n'
    )


def _repro_source(trace: SkillTrace, id8: str) -> str:
    """Render the repro module — ``repro_`` prefix keeps it invisible to load_suite."""
    payload = canonicalize(repr(trace.to_dict()))
    return (
        f'"""@generated failure repro {id8} — quarantined improvement target, never an anchor.\n'
        f'"""\n'
        "\n"
        f"TRACE = {payload}\n"
    )


def _read_manifest(evals_dir: Path) -> dict[str, Any]:
    try:
        raw = json.loads((evals_dir / ".manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_manifest(evals_dir: Path, manifest: dict[str, Any]) -> None:
    atomic_write_text(evals_dir / ".manifest.json", json.dumps(manifest, indent=2))


__all__ = ["PromotionResult", "canonicalize", "promote_traces", "retire_stale"]
