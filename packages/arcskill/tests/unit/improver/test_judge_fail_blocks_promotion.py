"""H-041 merge gate: a judge_rubric sandbox pass can NEVER be the promotion verdict.

A curated ``judge_rubric`` case cannot be scored inside the deterministic, network-isolated
golden-task sandbox (an LLM judge cannot run there). Its emitted pytest anchor therefore
checks only the case's *well-formedness* — so the sandbox returns a content-independent
PASS for it (the "always-pass"). The REAL judge verdict lives on a separate path
(:func:`evaluate_curated_case`) and fails closed.

These tests prove the two halves of the invariant the Planner requires:

1. The emitted judge_rubric anchor is well-formedness-only — the sandbox pass is genuinely
   content-independent (grounded in the REAL emitted artifact, not asserted by fiat).
2. That always-pass is INERT in the strict-improvement gate: a candidate producing bad
   output still "passes" the anchor in both before and after, so it is never a newly-passing
   case and can NEVER drive an acceptance. The sandbox pass is never the verdict of record.
3. The REAL judge verdict fails closed — a FAIL (or an unwired judge) is a ``passed=False``
   verdict of record, which is what actually blocks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcskill.improver.curation import emit_golden_case
from arcskill.improver.evalgate import EvalGate, load_suite
from arcskill.improver.goldencase import (
    CaseVerdict,
    CuratedGoldenCase,
    evaluate_curated_case,
    rubric_digest,
)
from arcskill.improver.models import BundleView, EvalOutcome

_RUBRIC = "Pass iff the summary names the customer AND the invoice amount."


class _FailingJudge:
    """A REAL judge whose verdict on the candidate is FAIL."""

    async def invoke(self, _prompt: str) -> str:
        return "FAIL"


class _AlwaysPassSandbox:
    """Faithful stand-in for the sandbox's verdict on a well-formedness-only anchor.

    The emitted judge_rubric anchor scores no candidate output (proven separately from the
    REAL artifact below), so the real golden-task harness returns PASS for it regardless of
    the candidate — a content-independent always-pass. This runner reproduces exactly that
    verdict, so the gate decision under test is the real strict-improvement logic.
    """

    async def run(self, _view: BundleView, cases: list[Any]) -> list[EvalOutcome]:
        return [
            EvalOutcome(case_id=c.id, passed=True, detail="well-formed (sandbox always-pass)")
            for c in cases
        ]


def _judge_case() -> CuratedGoldenCase:
    return CuratedGoldenCase(
        case_id="invoice-quality",
        skill_name="invoicer",
        gate_type="judge_rubric",
        rubric=_RUBRIC,
        judge_model_id="anthropic:claude-haiku",
        rubric_sha256=rubric_digest(_RUBRIC),
    )


def _emit_into(skill_dir: Path) -> Any:
    (skill_dir / "evals").mkdir(parents=True, exist_ok=True)
    return emit_golden_case(skill_dir, _judge_case())


def test_emitted_judge_rubric_anchor_is_content_independent(tmp_path: Path) -> None:
    """The sandbox anchor checks only the case's structure — it never scores a candidate.

    Grounds the "always-pass" claim in the REAL emitted artifact: the rubric prose never
    reaches the anchor, and no judge is invoked there, so the sandbox verdict cannot depend
    on candidate quality.
    """
    emitted = _emit_into(tmp_path)
    src = emitted.anchor_path.read_text(encoding="utf-8")

    # Structural well-formedness checks only.
    assert 'CASE["gate_type"]' in src
    assert 'CASE["provenance"] == "curated"' in src
    assert '"judge_model_id"' in src and '"rubric_sha256"' in src
    # The sandbox anchor NEVER sees the rubric prose and NEVER invokes a judge — so it
    # cannot possibly score the candidate. Its pass is content-independent.
    assert _RUBRIC not in src
    assert "invoke" not in src


@pytest.mark.asyncio
async def test_sandbox_always_pass_never_promotes(tmp_path: Path) -> None:
    """The judge_rubric always-pass is inert: it can never be the verdict of record.

    Even though the sandbox returns PASS for the judge_rubric anchor in BOTH before and
    after, the strict-improvement gate refuses to accept — the always-pass is never a
    newly-passing case, so a candidate producing worse output cannot be promoted on it.
    """
    _emit_into(tmp_path)
    cases = load_suite(tmp_path)
    assert cases, "the curated judge_rubric anchor must be discovered by load_suite"
    assert any(c.gate_type == "judge_rubric" and c.curated for c in cases)

    gate = EvalGate(_AlwaysPassSandbox(), min_golden_cases=1)
    decision = await gate.decide(
        before=BundleView("invoicer", "old prose", tmp_path),
        after=BundleView("invoicer", "candidate prose that yields WORSE output", tmp_path),
        cases=cases,
        tier="personal",
        kind="prose",
    )

    assert decision.accepted is False
    assert decision.newly_passing == 0
    assert "no strict improvement" in decision.reason


@pytest.mark.asyncio
async def test_real_judge_fail_is_the_failclosed_verdict_of_record() -> None:
    """The REAL judge verdict fails closed — a FAIL (or no judge) is ``passed=False``.

    This is the verdict of record for a judge_rubric case: the sandbox always-pass can never
    stand in for it, and a failing judge blocks.
    """
    case = _judge_case()

    fail_verdict = await evaluate_curated_case(
        case, "an unrelated output that ignores the rubric", judge=_FailingJudge()
    )
    assert isinstance(fail_verdict, CaseVerdict)
    assert fail_verdict.passed is False

    # No judge wired → fail-closed, never a silent pass (LLM09).
    no_judge = await evaluate_curated_case(case, "any output", judge=None)
    assert no_judge.passed is False
    assert "no judge" in no_judge.detail
