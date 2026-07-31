"""The JudgeAgreementSampler — COMP-013 / REQ-194.

Two claims are asserted here, and the benchmark's credibility rests on both:

* the sample is *fixed* — the same rows always yield the same questions, in any
  input order, in any process, under any `PYTHONHASHSEED`. A sample that
  redraws per run measures the sample as much as it measures the judge, which
  is why one test spends a subprocess proving the process-independence claim
  rather than asserting it in a comment;
* a label is never silently overwritten. When the judge's prompt or model
  changes, the label the row already carried is still on the row afterwards, in
  `prior_labels`, so old and new can be diffed (REQ-194).

Nothing here makes a network or LLM call: the re-grade seam is a scripted stub,
so the real selection, comparison and label-retention logic run for real. The
module under test does no file I/O at all; the one subprocess runs the repo's
own interpreter with no writable side effects.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

import pytest

from evaluations.longmemeval.agreement import (
    AgreementReport,
    JudgeAgreementSampler,
    selection_rank,
)
from evaluations.longmemeval.judge import Verdict
from evaluations.longmemeval.ledger import ResultRow

PROMPT_A = "Question: what did I say?\nAnswer: cilantro\nResponse: cilantro\n"
PROMPT_B = PROMPT_A + "\nAnswer only yes or no.\n"
MODEL_A = "gpt-4o-2024-08-06"
MODEL_B = "gpt-4o-2025-01-01"

REPO_ROOT = Path(__file__).resolve().parents[3]

_RANK_SCRIPT = """
import json
from evaluations.longmemeval.agreement import selection_rank

ids = [f"q{i}" for i in range(200)]
print(json.dumps(sorted(ids, key=selection_rank)[:12]))
"""


def make_verdict(
    *,
    correct: bool,
    prompt: str = PROMPT_A,
    model: str = MODEL_A,
) -> Verdict:
    """A verdict with the two fields that decide whether a label is superseded."""
    return Verdict(
        correct=correct,
        raw_response="yes" if correct else "no",
        prompt_used=prompt,
        judge_model_id=model,
    )


def make_row(
    question_id: str,
    *,
    correct: bool = True,
    prompt: str = PROMPT_A,
    model: str = MODEL_A,
    status: Literal["complete", "void", "error"] = "complete",
    verdict: Verdict | None = None,
    labelled: bool = True,
    prior_labels: list[dict[str, Any]] | None = None,
) -> ResultRow:
    """One finished row, labelled unless the status says it never got that far."""
    graded = verdict
    if graded is None and status == "complete" and labelled:
        graded = make_verdict(correct=correct, prompt=prompt, model=model)
    return ResultRow(
        question_id=question_id,
        status=status,
        question_type="single-session-user",
        is_abstention=False,
        answer="cilantro",
        verdict=graded.model_dump(mode="json") if graded is not None else None,
        prior_labels=prior_labels or [],
        provenance={"git_sha": "abc123"},
    )


class ScriptedJudge:
    """Stands in for a second judging pass, returning a per-question verdict.

    Deliberately not a mock of `JudgeAgent`: the sampler drives exactly one
    re-grade per sampled row, so a stub offering exactly that keeps the test
    honest about the seam and free of an LLM call.
    """

    def __init__(self, verdicts: dict[str, Verdict]) -> None:
        self._verdicts = verdicts
        self.seen: list[str] = []

    async def __call__(self, row: ResultRow) -> Verdict:
        self.seen.append(row.question_id)
        return self._verdicts[row.question_id]


def agreeing_judge(rows: list[ResultRow], **kwargs: Any) -> ScriptedJudge:
    """A judge that returns each row's own label back, under the given prompt/model."""
    return ScriptedJudge(
        {
            row.question_id: make_verdict(
                correct=Verdict.model_validate(row.verdict).correct, **kwargs
            )
            for row in rows
        }
    )


# ---------------------------------------------------------------------------
# The sample is fixed
# ---------------------------------------------------------------------------


def test_the_same_rows_always_yield_the_same_sample() -> None:
    rows = [make_row(f"q{index}") for index in range(40)]

    first = JudgeAgreementSampler(agreeing_judge(rows), sample_size=8).select(rows)
    second = JudgeAgreementSampler(agreeing_judge(rows), sample_size=8).select(rows)

    assert [row.question_id for row in first] == [row.question_id for row in second]


def test_input_order_does_not_change_the_sample() -> None:
    rows = [make_row(f"q{index}") for index in range(40)]
    sampler = JudgeAgreementSampler(agreeing_judge(rows), sample_size=8)

    forwards = [row.question_id for row in sampler.select(rows)]
    backwards = [row.question_id for row in sampler.select(list(reversed(rows)))]

    assert forwards == backwards


def test_selection_survives_a_changed_pythonhashseed() -> None:
    """The claim the whole component rests on: two runs sample the same questions.

    `hash()` over a str is seeded per process, so a selection built on it would
    pass every in-process assertion above and still redraw on the next run. The
    only way to prove it does not is to run it in another process under another
    seed.
    """
    ids = [f"q{index}" for index in range(200)]
    in_process = sorted(ids, key=selection_rank)[:12]

    ranked_under = [_rank_under_seed(seed) for seed in ("0", "1", "12345")]

    assert ranked_under == [in_process, in_process, in_process]


def _rank_under_seed(seed: str) -> list[str]:
    """Run the ranking in a fresh interpreter under an explicit hash seed."""
    env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(REPO_ROOT)}
    completed = subprocess.run(  # this interpreter, one literal script, no shell
        [sys.executable, "-c", _RANK_SCRIPT],
        capture_output=True,
        check=True,
        cwd=REPO_ROOT,
        env=env,
        text=True,
    )
    parsed: list[str] = json.loads(completed.stdout)
    return parsed


def test_sample_is_capped_at_the_requested_size() -> None:
    rows = [make_row(f"q{index}") for index in range(40)]

    assert len(JudgeAgreementSampler(agreeing_judge(rows), sample_size=5).select(rows)) == 5


def test_a_short_row_set_samples_every_candidate() -> None:
    rows = [make_row(f"q{index}") for index in range(3)]

    sample = JudgeAgreementSampler(agreeing_judge(rows), sample_size=50).select(rows)

    assert {row.question_id for row in sample} == {"q0", "q1", "q2"}


def test_only_labelled_complete_rows_are_candidates() -> None:
    rows = [
        make_row("complete"),
        make_row("voided", status="void"),
        make_row("errored", status="error"),
        make_row("unlabelled", status="complete", labelled=False),
    ]

    sample = JudgeAgreementSampler(ScriptedJudge({}), sample_size=10).select(rows)

    assert [row.question_id for row in sample] == ["complete"]


def test_a_repeated_question_id_samples_its_latest_row() -> None:
    """Repeats are this component's own output read back, not corruption."""
    rows = [make_row("q1", correct=True), make_row("q1", correct=False)]

    sample = JudgeAgreementSampler(ScriptedJudge({}), sample_size=10).select(rows)

    assert len(sample) == 1
    assert Verdict.model_validate(sample[0].verdict).correct is False


def test_sample_size_must_be_positive() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        JudgeAgreementSampler(ScriptedJudge({}), sample_size=0)


# ---------------------------------------------------------------------------
# The agreement rate is the metric
# ---------------------------------------------------------------------------


async def test_agreement_rate_counts_labels_that_matched() -> None:
    rows = [make_row(f"q{index}", correct=True) for index in range(4)]
    sample_ids = [
        row.question_id
        for row in JudgeAgreementSampler(ScriptedJudge({}), sample_size=4).select(rows)
    ]
    flipped = sample_ids[0]
    judge = ScriptedJudge(
        {
            question_id: make_verdict(correct=question_id != flipped)
            for question_id in sample_ids
        }
    )

    report, _ = await JudgeAgreementSampler(judge, sample_size=4).run(rows)

    assert report == AgreementReport(
        sample_size=4,
        agreement_rate=0.75,
        disagreements=[flipped],
    )
    assert sorted(judge.seen) == sorted(sample_ids)


async def test_a_sample_with_no_labelled_rows_reports_no_rate() -> None:
    """`None` reads as not measured; `0.0` reads as total disagreement."""
    rows = [make_row("voided", status="void")]

    report, rewritten = await JudgeAgreementSampler(ScriptedJudge({}), sample_size=4).run(rows)

    assert report == AgreementReport(sample_size=0, agreement_rate=None, disagreements=[])
    assert rewritten == []


# ---------------------------------------------------------------------------
# Prior labels are retained, never overwritten
# ---------------------------------------------------------------------------


async def test_a_changed_prompt_preserves_the_existing_label() -> None:
    original = make_verdict(correct=True, prompt=PROMPT_A)
    rows = [make_row("q1", verdict=original)]
    replacement = make_verdict(correct=False, prompt=PROMPT_B)

    report, rewritten = await JudgeAgreementSampler(
        ScriptedJudge({"q1": replacement}), sample_size=1
    ).run(rows)

    assert report.disagreements == ["q1"]
    assert len(rewritten) == 1
    assert Verdict.model_validate(rewritten[0].verdict) == replacement
    assert rewritten[0].prior_labels == [original.model_dump(mode="json")]


async def test_a_changed_model_preserves_the_existing_label() -> None:
    original = make_verdict(correct=True, model=MODEL_A)
    rows = [make_row("q1", verdict=original)]
    replacement = make_verdict(correct=True, model=MODEL_B)

    _, rewritten = await JudgeAgreementSampler(
        ScriptedJudge({"q1": replacement}), sample_size=1
    ).run(rows)

    assert Verdict.model_validate(rewritten[0].verdict) == replacement
    assert rewritten[0].prior_labels == [original.model_dump(mode="json")]


async def test_successive_judge_changes_accumulate_prior_labels() -> None:
    first = make_verdict(correct=True, prompt=PROMPT_A, model=MODEL_A)
    second = make_verdict(correct=False, prompt=PROMPT_B, model=MODEL_A)
    third = make_verdict(correct=True, prompt=PROMPT_B, model=MODEL_B)

    _, after_prompt = await JudgeAgreementSampler(
        ScriptedJudge({"q1": second}), sample_size=1
    ).run([make_row("q1", verdict=first)])
    _, after_model = await JudgeAgreementSampler(
        ScriptedJudge({"q1": third}), sample_size=1
    ).run(after_prompt)

    assert after_model[0].prior_labels == [
        first.model_dump(mode="json"),
        second.model_dump(mode="json"),
    ]
    assert Verdict.model_validate(after_model[0].verdict) == third


async def test_the_same_prompt_and_model_leaves_the_row_untouched() -> None:
    """A second opinion is not a supersession — overwriting would destroy the evidence."""
    original = make_verdict(correct=True)
    rows = [make_row("q1", verdict=original)]
    second_opinion = make_verdict(correct=False)

    report, rewritten = await JudgeAgreementSampler(
        ScriptedJudge({"q1": second_opinion}), sample_size=1
    ).run(rows)

    assert report.disagreements == ["q1"]
    assert rewritten == []
    assert Verdict.model_validate(rows[0].verdict) == original
    assert rows[0].prior_labels == []
