"""H-041c — a suite carrying a judge_rubric case can NEVER auto-promote.

The deterministic sandbox :class:`EvalRunner` can't run an LLM, so a ``judge_rubric``
case passes unchanged BEFORE and AFTER a candidate — it can neither cause nor PREVENT
promotion, waving every candidate through the strict-improvement gate on the exact /
assertion cases alone. The guard (locked "never automatic-AND-gated at once"): any
judge case in the suite forces the operator-review route, regardless of tier. Without an
approver the candidate is blocked fail-closed and audited; with one it is consulted (the
review route), not silently applied.

Driven through the REAL code-repair promote path (``_optimize_code``), the same path the
auto-improver runs — mirrors ``test_approval_gate.py`` but with a judge case in the suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcskill.improver import (
    ArcSkillImprover,
    CuratedGoldenCase,
    ImproverConfig,
    rubric_digest,
)
from arcskill.improver.models import BundlePatch, BundleView, EvalCase, EvalOutcome

_BUGGY = b"def add(a, b):\n    return a - b\n"
_FIXED = b"def add(a, b):\n    return a + b\n"
_RUBRIC = "Pass iff add() returns the sum."


class _FixMutator:
    async def propose(self, *, kind: str, current: BundleView, failures: str, insight: str):
        return BundlePatch(files={"scripts/calc.py": _FIXED}, summary="fix add")


class _PassRunner:
    """The sandbox: every case (judge case included) 'passes' on the fixed bundle.

    This is exactly the hole — the runner can't score a judge case, so it reports it
    passed on both bundles, waving it through the strict-improvement gate.
    """

    async def run(self, view: BundleView, cases: list[EvalCase]) -> list[EvalOutcome]:
        fixed = _FIXED in view.scripts.values()
        return [EvalOutcome(case_id=c.id, passed=fixed) for c in cases]


class _Approver:
    def __init__(self, grant: bool) -> None:
        self.grant = grant
        self.calls: list[str] = []
        self.details: list[str] = []

    async def __call__(self, action: str, skill_name: str, detail: str) -> bool:
        self.calls.append(action)
        self.details.append(detail)
        return self.grant


class _Sink:
    def __init__(self) -> None:
        self.events: list[object] = []

    def write(self, event: object) -> None:
        self.events.append(event)


def _seed(root: Path) -> Path:
    sk = root / "s"
    (sk / "scripts").mkdir(parents=True)
    (sk / "evals").mkdir(parents=True)
    (sk / "SKILL.md").write_text("# s\n", encoding="utf-8")
    (sk / "scripts" / "calc.py").write_bytes(_BUGGY)
    # A machine-authored exact anchor so the strict gate has an auto-scorable case to
    # flip fail->pass; the judge case rides alongside it in the same suite.
    (sk / "evals" / "test_g.py").write_text("def test_a():\n    assert 1\n", encoding="utf-8")
    return sk / "SKILL.md"


def _make(
    root: Path, skill_md: Path, *, approval_provider: object | None, sink: _Sink
) -> ArcSkillImprover:
    imp = ArcSkillImprover(
        root / "ws",
        config=ImproverConfig(
            min_traces=1, trace_buffer_turns=0, optimize_after_uses=1, min_golden_cases=1
        ),
        tier="personal",  # the tier that WOULD auto-apply — where the hole bites hardest
        mutator=_FixMutator(),
        eval_runner=_PassRunner(),
        approval_provider=approval_provider,
        audit_sink=sink,
        skill_path=lambda name: skill_md,
    )
    # Emit a judge_rubric curated case INTO the suite (pinned judge + rubric sha256).
    imp.curate_golden(
        CuratedGoldenCase(
            case_id="quality",
            skill_name="s",
            gate_type="judge_rubric",
            rubric=_RUBRIC,
            judge_model_id="anthropic:claude-haiku",
            rubric_sha256=rubric_digest(_RUBRIC),
        )
    )
    return imp


async def _drive(imp: ArcSkillImprover) -> None:
    await imp.observe(skill_name="s", tool_name="run", status="error", error_type="AssertionError")
    await imp.on_turn_end(turn=0, outcome="failure")
    await imp.maybe_improve()
    await imp.aclose()


@pytest.mark.asyncio
async def test_judge_case_blocks_personal_auto_promotion_without_approver(tmp_path: Path) -> None:
    """Personal tier WOULD auto-apply — but a judge case forces review; no approver = blocked.

    Contrast with ``test_approval_gate.test_personal_code_needs_no_approval`` (identical
    setup MINUS the judge case) which auto-applies. The judge case is the sole difference.
    """
    skill_md = _seed(tmp_path)
    sink = _Sink()
    imp = _make(tmp_path, skill_md, approval_provider=None, sink=sink)

    await _drive(imp)

    # Fail-closed: the candidate did NOT auto-promote despite passing the strict gate.
    assert (skill_md.parent / "scripts" / "calc.py").read_bytes() == _BUGGY
    # Honest routing: a pending-review/blocked audit event, not a silent skip.
    denied = [e for e in sink.events if getattr(e, "outcome", "") == "denied_no_approver"]
    assert denied, "the judge-gated candidate must be routed to review and audited, not dropped"
    # The outcome carries WHY (the judge case that forced review) + how to get real verdicts.
    detail = str(denied[0].extra.get("detail", ""))
    assert "judge_rubric" in detail
    assert "arc skill evals judge" in detail


@pytest.mark.asyncio
async def test_judge_case_routes_to_operator_review_when_approver_wired(tmp_path: Path) -> None:
    """With an approver wired, the judge-gated candidate is CONSULTED, then applied on grant."""
    skill_md = _seed(tmp_path)
    sink = _Sink()
    approver = _Approver(grant=True)
    imp = _make(tmp_path, skill_md, approval_provider=approver, sink=sink)

    await _drive(imp)

    # It went through the operator-review ladder (not auto-applied), then applied on grant.
    assert approver.calls == ["skill.mutation"], "the candidate must route to operator review"
    # The operator saw WHY it needed review + the command to compute the real verdicts.
    assert "judge_rubric" in approver.details[0]
    assert "arc skill evals judge" in approver.details[0]
    assert (skill_md.parent / "scripts" / "calc.py").read_bytes() == _FIXED
