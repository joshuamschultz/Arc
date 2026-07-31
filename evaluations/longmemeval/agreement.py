"""JudgeAgreementSampler (COMP-013) — how far the judge disagrees with itself.

``temperature = 0`` does not make an LLM judge deterministic. It makes it
*mostly* repeatable, and the items it is not repeatable on are exactly the
borderline ones a benchmark number turns on. So the agreement rate is measured
and published beside the accuracy rather than assumed to be 1.0 (REQ-194).

**The sample is fixed, never drawn fresh.** Two runs over the same results must
double-judge the same questions, or the agreement rate is measuring the sample
as much as the judge. Selection is a pure function of the question id — see
:func:`selection_rank` for why it is a digest and not a seeded shuffle.

**Prior labels are retained, never overwritten** (REQ-194). When a re-grade
comes back from a different prompt or a different model, the label the row
already carried moves into ``prior_labels`` and the new one becomes current, so
the two can be diffed. When the prompt and model are unchanged the row's label
stands: nothing superseded it, and the second opinion is a measurement of the
judge's own spread, which is what the report is for.

This module knows nothing about the dataset. The caller hands in a re-grade
closure that already knows where the question text and the gold field live,
which keeps grading policy in COMP-010 and leaves this component as arithmetic
over rows plus one call per sampled question.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from evaluations.longmemeval.judge import Verdict
from evaluations.longmemeval.ledger import DONE_STATUS, ResultRow

_RANK_DIGEST_SIZE = 8
"""Bytes of blake2b kept as the selection rank. 64 bits is far past what a
500-question dataset needs to avoid collisions, and the id breaks any tie."""


class Regrade(Protocol):
    """Grade one already-answered row a second time.

    Narrow on purpose: the sampler needs a fresh :class:`Verdict` for a row it
    picked, and nothing else. The caller supplies the closure — it is the one
    that knows the dataset, builds the judge agent and decides which prompt and
    model this pass runs under.
    """

    async def __call__(self, row: ResultRow) -> Verdict: ...


class AgreementReport(BaseModel):
    """How often the judge gave the same answer twice, over the fixed sample.

    ``sample_size`` is the number of questions actually double-judged, which is
    the requested size or the number of labelled rows available, whichever is
    smaller. ``agreement_rate`` is ``None`` rather than ``0.0`` when nothing was
    sampled — a rate with no denominator is not a result, and ``0.0`` would read
    as total disagreement to anyone who published it.
    """

    model_config = ConfigDict(frozen=True)

    sample_size: int
    agreement_rate: float | None
    disagreements: list[str]


def selection_rank(question_id: str) -> str:
    """The fixed rank a question sorts by when the sample is drawn.

    ``blake2b`` over the id and nothing else. Three properties make the sample
    reproducible, and each one rules out an alternative that looks equivalent:

    * it does not depend on the process, so two runs draw the same questions —
      Python's built-in ``hash()`` is seeded per process by ``PYTHONHASHSEED``
      and would silently redraw the sample on every invocation;
    * it does not depend on the library version, unlike ``random.sample`` under
      a fixed seed, whose stream is explicitly not a compatibility guarantee;
    * it does not depend on the other candidates, so membership is stable as the
      results file grows — a seeded shuffle of the candidate list reshuffles
      every position the moment one row is appended.
    """
    return hashlib.blake2b(
        question_id.encode("utf-8"), digest_size=_RANK_DIGEST_SIZE
    ).hexdigest()


class JudgeAgreementSampler:
    """Double-judges a fixed sample and reports the agreement rate (COMP-013)."""

    def __init__(self, regrade: Regrade, *, sample_size: int) -> None:
        if sample_size < 1:
            raise ValueError(f"sample_size must be at least 1, got {sample_size}")
        self._regrade = regrade
        self._sample_size = sample_size

    def select(self, rows: Sequence[ResultRow]) -> list[ResultRow]:
        """The questions this row set yields — same rows in, same questions out.

        Input order is irrelevant: rows are ordered by :func:`selection_rank`,
        so a results file read back in a different order samples identically.
        Only rows that reached ``complete`` *and* carry a verdict are eligible,
        because a voided or errored question has no label to agree with.
        """
        candidates = _latest_labelled(rows)
        ordered = sorted(
            candidates,
            key=lambda row: (selection_rank(row.question_id), row.question_id),
        )
        return ordered[: self._sample_size]

    async def run(self, rows: Sequence[ResultRow]) -> tuple[AgreementReport, list[ResultRow]]:
        """Re-grade the fixed sample; return the report and the rewritten rows.

        The returned rows are only those whose label was superseded by a changed
        prompt or model, carrying their previous label in ``prior_labels``. They
        are new objects — :class:`ResultRow` is frozen, and the ledger is
        append-only (REQ-206), so a superseded row is appended, never edited in
        place.
        """
        sample = self.select(rows)
        disagreements: list[str] = []
        rewritten: list[ResultRow] = []
        agreed = 0

        for row in sample:
            first = Verdict.model_validate(row.verdict)
            second = await self._regrade(row)

            if first.correct == second.correct:
                agreed += 1
            else:
                disagreements.append(row.question_id)

            if _supersedes(first, second):
                rewritten.append(
                    row.model_copy(
                        update={
                            "verdict": second.model_dump(mode="json"),
                            "prior_labels": [*row.prior_labels, first.model_dump(mode="json")],
                        }
                    )
                )

        report = AgreementReport(
            sample_size=len(sample),
            agreement_rate=agreed / len(sample) if sample else None,
            disagreements=disagreements,
        )
        return report, rewritten


def _supersedes(first: Verdict, second: Verdict) -> bool:
    """Whether the second verdict replaces the first rather than seconding it.

    A different prompt or a different model is a different measurement, so its
    label supersedes. The same prompt on the same model is a second opinion on
    the same measurement, and overwriting the first with it would destroy the
    evidence the agreement rate is built from.
    """
    return (
        first.prompt_used != second.prompt_used or first.judge_model_id != second.judge_model_id
    )


def _latest_labelled(rows: Sequence[ResultRow]) -> list[ResultRow]:
    """Eligible rows, one per question, keeping the last of any repeats.

    Repeats are normal, not corruption: this component's own rewritten rows are
    appended to the same file, so reading it back yields a question's original
    label followed by its superseded one. The file is chronological, so the last
    occurrence is the current label.
    """
    latest: dict[str, ResultRow] = {}
    for row in rows:
        if row.status == DONE_STATUS and row.verdict is not None:
            latest[row.question_id] = row
    return list(latest.values())


__all__ = [
    "AgreementReport",
    "JudgeAgreementSampler",
    "Regrade",
    "selection_rank",
]
