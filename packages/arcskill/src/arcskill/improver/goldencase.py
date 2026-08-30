"""Curated golden cases — gate type is per-case DATA, compared per type (H-041).

The operator-facing improvement loop turns a real used trace into a *golden case*:
an ideal result the skill should reproduce. A case's ``gate_type`` is a value on the
case, never a code branch (D — locked design §1):

* ``exact_match``   — deterministic tool output equals the recorded ideal.
* ``assertions``    — operator-written structured checks all hold on the output.
* ``judge_rubric``  — a semantic/prose rubric an LLM judge scores pass/fail.

``judge_rubric`` is only reproducible if the judge is PINNED: the case records the
judge model id and the sha256 of the exact rubric text. A ``judge_rubric`` pass with
either missing is INVALID (``PinnedJudgeError``) — that is what makes "strict
improvement" a fact about a fixed judge, not the judge's mood (locked design §2).

This module owns the case model, its validation, and the per-type comparison. It is
provider-free: the judge enters through the injected :class:`~arcskill.improver.seams.LLMInvoker`.
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from arcskill.improver.seams import LLMInvoker

GateType = Literal["exact_match", "assertions", "judge_rubric"]
GATE_TYPES: frozenset[str] = frozenset({"exact_match", "assertions", "judge_rubric"})

# A judge verdict is accepted only when its (normalized) first word is exactly this.
# Anything else — including an empty or hedged reply — fails closed (LLM09).
_VERDICT_OK = "pass"


class CurationError(ValueError):
    """A curated golden case is malformed or cannot be safely emitted."""


class PinnedJudgeError(CurationError):
    """A ``judge_rubric`` case is missing its pinned judge model id or rubric sha256.

    Raised at emission AND at evaluation: a semantic gate that does not record the
    exact judge + rubric it was scored against can never be reproduced, so it must
    never silently pass (locked design §2).
    """


class AssertionCheck(BaseModel):
    """One operator-written check for an ``assertions`` gate — structured, not code.

    Deterministic and side-effect-free by construction (no ``eval``/exec of operator
    text — ASI05/LLM05): the four kinds are all pure string predicates.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["contains", "not_contains", "equals", "regex"]
    value: str

    def holds(self, output: str) -> bool:
        if self.kind == "contains":
            return self.value in output
        if self.kind == "not_contains":
            return self.value not in output
        if self.kind == "equals":
            return _normalize(output) == _normalize(self.value)
        return re.search(self.value, output) is not None


class CuratedGoldenCase(BaseModel):
    """A golden case emitted by the operator curation loop (H-041).

    Pydantic at the boundary (config/artifact). ``gate_type`` carries the comparison
    contract as data; the per-type params (``ideal_output`` / ``assertions`` /
    ``rubric`` + pinned judge) are populated for the matching type only.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    skill_name: str
    gate_type: GateType
    source_trace_id: str = ""
    # The operator-edited ideal (exact_match) — the result the skill *should* produce.
    ideal_output: str = ""
    # Operator-written checks (assertions).
    assertions: list[AssertionCheck] = Field(default_factory=list)
    # Semantic rubric prose + its pinned judge (judge_rubric only).
    rubric: str = ""
    judge_model_id: str = ""
    rubric_sha256: str = ""
    provenance: Literal["curated"] = "curated"

    def validate_pinned(self) -> None:
        """Fail-closed structural check; raises :class:`CurationError` on any gap.

        For ``judge_rubric`` the judge id + rubric sha256 must both be present and the
        recorded sha256 must match the recorded rubric bytes (no swap after pinning).
        """
        if self.gate_type not in GATE_TYPES:
            raise CurationError(f"unknown gate_type {self.gate_type!r}")
        if self.gate_type == "exact_match" and not self.ideal_output:
            raise CurationError("exact_match case requires a non-empty ideal_output")
        if self.gate_type == "assertions" and not self.assertions:
            raise CurationError("assertions case requires at least one check")
        if self.gate_type == "judge_rubric":
            if not self.rubric:
                raise CurationError("judge_rubric case requires a rubric")
            if not self.judge_model_id or not self.rubric_sha256:
                raise PinnedJudgeError(
                    "judge_rubric case must pin both judge_model_id and rubric_sha256"
                )
            if self.rubric_sha256 != rubric_digest(self.rubric):
                raise PinnedJudgeError("rubric_sha256 does not match the recorded rubric bytes")


class CaseVerdict(BaseModel):
    """Result of comparing a candidate output against one curated case."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    passed: bool
    detail: str = ""


def rubric_digest(rubric: str) -> str:
    """The pin: sha256 of the exact rubric text (utf-8)."""
    return hashlib.sha256(rubric.encode("utf-8")).hexdigest()


def _normalize(text: str) -> str:
    """Whitespace-normalized comparison key so trailing/indent drift is not a diff."""
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


async def evaluate_curated_case(
    case: CuratedGoldenCase,
    candidate_output: str,
    *,
    judge: LLMInvoker | None = None,
) -> CaseVerdict:
    """Compare ``candidate_output`` against ``case`` PER its ``gate_type`` (H-041).

    The dispatch is on the case's data, never a caller branch. ``judge_rubric``
    fails closed: the pin is re-validated here (never trust that emission validated
    it), and a missing judge or a non-"pass" verdict is a fail — a mood-free rule.
    """
    case.validate_pinned()
    if case.gate_type == "exact_match":
        passed = _normalize(candidate_output) == _normalize(case.ideal_output)
        return CaseVerdict(
            case_id=case.case_id,
            passed=passed,
            detail="exact match" if passed else "output differs from ideal",
        )
    if case.gate_type == "assertions":
        failed = [c for c in case.assertions if not c.holds(candidate_output)]
        return CaseVerdict(
            case_id=case.case_id,
            passed=not failed,
            detail="all checks hold"
            if not failed
            else f"{len(failed)} of {len(case.assertions)} check(s) failed",
        )
    # judge_rubric — the pin is already validated above.
    if judge is None:
        return CaseVerdict(
            case_id=case.case_id, passed=False, detail="no judge wired (fail-closed)"
        )
    verdict = await judge.invoke(_judge_prompt(case, candidate_output))
    passed = verdict.strip().lower().split()[:1] == [_VERDICT_OK]
    return CaseVerdict(
        case_id=case.case_id,
        passed=passed,
        detail=f"judge={case.judge_model_id} verdict={verdict.strip()[:80]!r}",
    )


def _judge_prompt(case: CuratedGoldenCase, candidate_output: str) -> str:
    """Render the pinned rubric + candidate into a strict PASS/FAIL judge prompt."""
    return (
        "You are a strict evaluator. Apply the rubric to the candidate output.\n"
        "Answer with exactly one word on the first line: PASS or FAIL.\n\n"
        f"RUBRIC:\n{case.rubric}\n\n"
        f"CANDIDATE OUTPUT:\n{candidate_output}\n"
    )


__all__ = [
    "GATE_TYPES",
    "AssertionCheck",
    "CaseVerdict",
    "CuratedGoldenCase",
    "CurationError",
    "GateType",
    "PinnedJudgeError",
    "evaluate_curated_case",
    "rubric_digest",
]
