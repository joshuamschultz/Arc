"""Gate type is per-case DATA, compared per type, with a pinned judge (H-041)."""

from __future__ import annotations

import pytest
from arcskill.improver.goldencase import (
    AssertionCheck,
    CuratedGoldenCase,
    PinnedJudgeError,
    evaluate_curated_case,
    rubric_digest,
)


class _FakeJudge:
    """Deterministic LLMInvoker: returns whatever verdict it was seeded with."""

    def __init__(self, verdict: str) -> None:
        self.verdict = verdict
        self.calls: list[str] = []

    async def invoke(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self.verdict


def _exact(**kw: object) -> CuratedGoldenCase:
    return CuratedGoldenCase(
        case_id="c1", skill_name="s", gate_type="exact_match", ideal_output="the answer: 42", **kw
    )


async def test_exact_match_gate_compares_normalized_output() -> None:
    case = _exact()
    ok = await evaluate_curated_case(case, "the answer: 42\n")
    bad = await evaluate_curated_case(case, "the answer: 43")
    assert ok.passed and not bad.passed


async def test_assertions_gate_runs_every_structured_check() -> None:
    case = CuratedGoldenCase(
        case_id="c2",
        skill_name="s",
        gate_type="assertions",
        assertions=[
            AssertionCheck(kind="contains", value="INVOICE"),
            AssertionCheck(kind="not_contains", value="ERROR"),
        ],
    )
    assert (await evaluate_curated_case(case, "INVOICE #7 total $10")).passed
    assert not (await evaluate_curated_case(case, "INVOICE but also ERROR")).passed


async def test_judge_rubric_gate_uses_the_injected_judge() -> None:
    rubric = "Pass iff the summary names the customer and the amount."
    case = CuratedGoldenCase(
        case_id="c3",
        skill_name="s",
        gate_type="judge_rubric",
        rubric=rubric,
        judge_model_id="anthropic:claude-haiku",
        rubric_sha256=rubric_digest(rubric),
    )
    passing = _FakeJudge("PASS — names both")
    failing = _FakeJudge("FAIL")
    assert (await evaluate_curated_case(case, "Acme owes $10", judge=passing)).passed
    assert not (await evaluate_curated_case(case, "something", judge=failing)).passed
    assert passing.calls  # the pinned judge was actually consulted


async def test_gate_type_is_data_not_a_caller_branch() -> None:
    """The same evaluate call dispatches purely on the case's gate_type field."""
    rubric = "r"
    cases = [
        _exact(),
        CuratedGoldenCase(
            case_id="a", skill_name="s", gate_type="assertions",
            assertions=[AssertionCheck(kind="equals", value="x")],
        ),
        CuratedGoldenCase(
            case_id="j", skill_name="s", gate_type="judge_rubric", rubric=rubric,
            judge_model_id="m", rubric_sha256=rubric_digest(rubric),
        ),
    ]
    judge = _FakeJudge("PASS")
    verdicts = [await evaluate_curated_case(c, "x", judge=judge) for c in cases]
    assert [v.case_id for v in verdicts] == ["c1", "a", "j"]


# --- NAMED TEST 2: a judge_rubric case without a pinned judge is INVALID ---------


async def test_judge_rubric_without_pinned_judge_id_is_rejected() -> None:
    rubric = "some rubric"
    case = CuratedGoldenCase(
        case_id="c4",
        skill_name="s",
        gate_type="judge_rubric",
        rubric=rubric,
        judge_model_id="",  # missing judge id
        rubric_sha256=rubric_digest(rubric),
    )
    with pytest.raises(PinnedJudgeError):
        await evaluate_curated_case(case, "anything", judge=_FakeJudge("PASS"))


async def test_judge_rubric_without_rubric_sha256_is_rejected() -> None:
    case = CuratedGoldenCase(
        case_id="c5",
        skill_name="s",
        gate_type="judge_rubric",
        rubric="some rubric",
        judge_model_id="m",
        rubric_sha256="",  # missing pin
    )
    with pytest.raises(PinnedJudgeError):
        await evaluate_curated_case(case, "anything", judge=_FakeJudge("PASS"))


async def test_judge_rubric_with_swapped_rubric_bytes_is_rejected() -> None:
    """The pin must match the recorded rubric — a post-pin swap is invalid (no TOCTOU)."""
    case = CuratedGoldenCase(
        case_id="c6",
        skill_name="s",
        gate_type="judge_rubric",
        rubric="the REAL rubric",
        judge_model_id="m",
        rubric_sha256=rubric_digest("a DIFFERENT rubric"),
    )
    with pytest.raises(PinnedJudgeError):
        await evaluate_curated_case(case, "anything", judge=_FakeJudge("PASS"))
