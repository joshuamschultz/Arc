"""Helpers for the operator controls on :class:`ArcSkillImprover` (alpha-2 P8).

The improver owns the orchestration (``improve_now`` / ``run_evals`` /
``regen_evals``); this module holds the pure pieces it needs so the facade stays
readable: the bounded in-memory preview stash, the result shapes, the unified
diff, and the regen guard. Results are plain ``dict`` values because the
``SkillAdapter`` seam speaks primitives only.

Statuses a control can return (the route maps them to HTTP codes):

``preview`` · ``applied`` · ``rejected`` · ``denied`` · ``completed`` ·
``no_change`` · ``no_candidate`` · ``no_suite`` · ``not_found`` · ``unavailable`` ·
``retired`` · ``exempt`` · ``insufficient_traces`` · ``busy`` · ``timeout`` ·
``preview_expired`` · ``stale_preview`` · ``refused``.
"""

from __future__ import annotations

import difflib
import hashlib
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arcskill.improver.evalgate import GateDecision, load_suite
from arcskill.improver.models import Candidate, EvalCase, EvalOutcome

#: Fewest usage traces a manual pass needs: the engine splits traces into a train
#: and a holdout set, and needs at least one in each.
MIN_MANUAL_TRACES = 2
_GENERATED_NAME = "test_golden_generated.py"
_PREVIEW_TTL_S = 900.0
_PREVIEW_MAX = 16


def status_result(status: str, skill_name: str, reason: str = "") -> dict[str, Any]:
    """A control result with no payload beyond its status and reason."""
    return {"status": status, "skill_name": skill_name, "reason": reason}


def gate_payload(decision: GateDecision) -> dict[str, Any]:
    return {
        "accepted": decision.accepted,
        "reason": decision.reason,
        "before_pass": decision.before_pass,
        "after_pass": decision.after_pass,
        "newly_passing": decision.newly_passing,
    }


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def unified_diff(before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="current/SKILL.md",
            tofile="candidate/SKILL.md",
        )
    )


@dataclass
class Preview:
    """A previewed candidate the operator may apply while the skill is unchanged."""

    skill_name: str
    base_sha256: str
    candidate: Candidate
    seed_scores: dict[str, float]
    trace_ids: list[str]
    created: float = field(default_factory=time.monotonic)


class PreviewStash:
    """Bounded, short-lived previews keyed by an unguessable id (in memory only).

    A preview is taken once: apply consumes it, so a replayed apply finds nothing.
    """

    def __init__(self) -> None:
        self._items: OrderedDict[str, Preview] = OrderedDict()

    def put(self, preview: Preview) -> str:
        self._evict()
        preview_id = uuid.uuid4().hex
        self._items[preview_id] = preview
        while len(self._items) > _PREVIEW_MAX:
            self._items.popitem(last=False)
        return preview_id

    def take(self, preview_id: str, skill_name: str) -> Preview | None:
        self._evict()
        preview = self._items.get(preview_id)
        if preview is None or preview.skill_name != skill_name:
            return None
        del self._items[preview_id]
        return preview

    def _evict(self) -> None:
        now = time.monotonic()
        for key in [k for k, p in self._items.items() if now - p.created > _PREVIEW_TTL_S]:
            del self._items[key]


def provenance(case: EvalCase) -> str:
    if case.curated:
        return "curated"
    return "machine" if case.machine_authored else "human"


def eval_run_result(
    skill_name: str, cases: list[EvalCase], outcomes: list[EvalOutcome]
) -> dict[str, Any]:
    """Pass/fail per case; a case the runner did not report counts as failed."""
    by_id = {o.case_id: o for o in outcomes}
    rows = []
    for case in cases:
        outcome = by_id.get(case.id)
        rows.append(
            {
                "case_id": case.id,
                "passed": bool(outcome and outcome.passed),
                "detail": outcome.detail if outcome else "no result from the eval runner",
                "gate_type": case.gate_type,
                "provenance": provenance(case),
            }
        )
    passed = sum(1 for row in rows if row["passed"])
    return {
        "status": "completed",
        "skill_name": skill_name,
        "total": len(rows),
        "passed": passed,
        "failed": len(rows) - passed,
        "cases": rows,
    }


def regen_refusal(skill_name: str, skill_dir: Path) -> dict[str, Any] | None:
    """Refuse to regenerate over a generated file a human has edited (REQ-111)."""
    edited = [
        case
        for case in load_suite(skill_dir)
        if case.id.split("::", 1)[0] == f"evals/{_GENERATED_NAME}" and not case.machine_authored
    ]
    if not edited:
        return None
    return status_result(
        "refused",
        skill_name,
        f"{_GENERATED_NAME} was edited by a person; regenerating would overwrite that "
        "work. Move the edited cases to their own file first.",
    )


def audit_extra(result: dict[str, Any]) -> dict[str, Any]:
    """What a control audit event records: status, reason, ids, verdict — never a body."""
    keep = ("status", "reason", "candidate_id", "preview_id", "total", "passed", "failed")
    extra = {k: result[k] for k in keep if k in result}
    gate = result.get("gate")
    if isinstance(gate, dict):
        extra["gate_accepted"] = gate.get("accepted")
        extra["gate_reason"] = gate.get("reason")
    return extra


__all__ = [
    "MIN_MANUAL_TRACES",
    "Preview",
    "PreviewStash",
    "audit_extra",
    "eval_run_result",
    "gate_payload",
    "provenance",
    "regen_refusal",
    "sha256_text",
    "status_result",
    "unified_diff",
]
