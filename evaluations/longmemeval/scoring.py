"""ScoringEngine (COMP-012) — the numbers the benchmark actually defines.

Pure arithmetic over finished rows: no I/O, no network, no agent. Everything
here is a decision about *what to divide by*, which is the only place a
benchmark number goes quietly wrong.

**Three accuracies, three denominators** (REQ-192). ``task_averaged_accuracy``
is the macro mean over the six question types — every type weighs the same, so
a large stratum cannot carry a small one. ``overall_accuracy`` is the micro mean
over every scored question, which is a different number whenever the strata are
unbalanced, and LongMemEval's are. ``abstention_accuracy`` is reported on its
own because refusing to answer is a different skill from answering, and
averaging it into either figure hides both.

**``_abs`` is a cross-tag, never a seventh type** (REQ-190). Its QA result folds
into the base question type it was written from, so the macro mean stays a mean
over six. It is excluded from retrieval entirely: ``answer_session_ids`` is
empty for an unanswerable question, and scoring recall against no gold would be
either vacuously perfect or meaninglessly zero.

**Void rows are excluded from every figure and counted separately.** A question
voided because the input filter ate its gold evidence (COMP-005) was never
measured; scoring it as wrong would charge arcmemory for the harness's own
damage, and dropping it silently would hide how much of the benchmark that is.

**Rates with a zero denominator are absent, not zero.** ``None`` reads as "not
measured"; ``0.0`` reads as a result, and someone would publish it.

**The interval is Wilson, not the normal approximation** (REQ-193). At the sizes
these strata actually reach, the normal approximation runs off the end of [0, 1]
and is symmetric where the truth is not: at n=13 and 10 correct the honest
interval is roughly 50%-92%, which cannot separate two systems — which is
exactly why anything under 30 scored questions is labelled ``directional``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evaluations.longmemeval.judge import Verdict, is_abstention_question
from evaluations.longmemeval.reference_prompts import UnknownQuestionTypeError

QUESTION_TYPES: tuple[str, ...] = (
    "single-session-user",
    "single-session-assistant",
    "single-session-preference",
    "multi-session",
    "temporal-reasoning",
    "knowledge-update",
)
"""The six types the macro mean is taken over. ``_abs`` is not among them."""

DIRECTIONAL_MIN_N = 30
"""Below this many scored questions a per-type accuracy is directional only."""

DEFAULT_K_VALUES: tuple[int, ...] = (1, 3, 5, 10)
"""The cut-offs recall is reported at. Every one is named in the output."""

Z_95 = 1.959963984540054
"""The standard normal quantile for a two-sided 95% interval."""

_Z_95_SQUARED = Z_95 * Z_95

RowStatus = Literal["complete", "void", "error"]


class WilsonInterval(BaseModel):
    """A closed interval on a proportion, clamped to [0, 1]."""

    model_config = ConfigDict(frozen=True)

    lower: float
    upper: float


class ScoredRow(BaseModel):
    """The scoring-relevant projection of one finished result row.

    Narrower than the ledger's row on purpose: scoring needs the verdict, the
    stratum and the gold flags, and nothing else it reads can drift. Unknown
    fields are ignored, so a full ledger row validates into this shape directly.

    ``retrieved_turn_ids`` is *ranked*, best first — recall@k means nothing
    otherwise. Session-level ranking is derived from it rather than carried
    alongside, because two lists that can disagree eventually do.
    """

    model_config = ConfigDict(frozen=True)

    question_id: str
    question_type: str
    status: RowStatus
    verdict: Verdict | None = None
    gold_turn_ids: list[str] = Field(default_factory=list)
    gold_session_ids: list[str] = Field(default_factory=list)
    retrieved_turn_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _complete_rows_carry_a_verdict(self) -> ScoredRow:
        """A complete row without a verdict would score as wrong and understate accuracy."""
        if self.status == "complete" and self.verdict is None:
            raise ValueError(f"row {self.question_id!r} is complete but carries no verdict")
        return self

    @property
    def is_abstention(self) -> bool:
        """Derived from the question id, which is the rule itself (REQ-190)."""
        return is_abstention_question(self.question_id)

    @property
    def correct(self) -> bool:
        """Whether the judge passed this answer. Only meaningful on a complete row."""
        return self.verdict is not None and self.verdict.correct


class TypeAccuracy(BaseModel):
    """One stratum's accuracy, with the interval and the honesty flag beside it."""

    model_config = ConfigDict(frozen=True)

    question_type: str
    n: int
    accuracy: float
    wilson_95_ci: WilsonInterval
    directional: bool


class RecallAtK(BaseModel):
    """Recall at one cut-off, at one granularity.

    ``recall_any`` is the share of questions with at least one gold unit in the
    top k; ``recall_all`` is the share with every gold unit there. Reporting one
    without the other is how "recall" stops being comparable.
    """

    model_config = ConfigDict(frozen=True)

    k: int
    n: int
    recall_any: float | None
    recall_all: float | None


class RetrievalReport(BaseModel):
    """Recall at both granularities the benchmark's gold flags support."""

    model_config = ConfigDict(frozen=True)

    turn: list[RecallAtK]
    session: list[RecallAtK]


class Report(BaseModel):
    """Everything a run publishes about its own accuracy."""

    model_config = ConfigDict(frozen=True)

    task_averaged_accuracy: float | None
    overall_accuracy: float | None
    abstention_accuracy: float | None
    scored_n: int
    abstention_n: int
    void_n: int
    error_n: int
    per_type: list[TypeAccuracy]
    retrieval: RetrievalReport
    k_values: list[int]


def wilson_interval(successes: int, n: int) -> WilsonInterval:
    """The Wilson score interval at 95%, clamped to [0, 1].

    Wilson rather than the normal approximation because these strata are small
    and the observed rates are far from 0.5, where the approximation is both
    too narrow and wrongly symmetric — and where it happily reports bounds
    outside [0, 1] that a reader would have to know to distrust.
    """
    if n <= 0:
        raise ValueError("n must be positive; a rate with no denominator has no interval")
    if not 0 <= successes <= n:
        raise ValueError(f"successes must lie in [0, {n}], got {successes}")

    proportion = successes / n
    denominator = 1.0 + _Z_95_SQUARED / n
    center = (proportion + _Z_95_SQUARED / (2 * n)) / denominator
    spread = math.sqrt(proportion * (1.0 - proportion) / n + _Z_95_SQUARED / (4.0 * n * n))
    half_width = (Z_95 / denominator) * spread
    return WilsonInterval(
        lower=max(0.0, center - half_width),
        upper=min(1.0, center + half_width),
    )


class ScoringEngine:
    """Aggregates finished rows into the report a run publishes (COMP-012)."""

    def __init__(self, *, k_values: Sequence[int] = DEFAULT_K_VALUES) -> None:
        if not k_values or list(k_values) != sorted(set(k_values)) or k_values[0] < 1:
            raise ValueError(f"k values must be positive, unique and ascending, got {k_values!r}")
        self._k_values = tuple(k_values)

    def score(self, rows: Iterable[ScoredRow]) -> Report:
        """Aggregate rows into the three accuracies, the strata and the recall table."""
        all_rows = list(rows)
        for row in all_rows:
            if row.question_type not in QUESTION_TYPES:
                raise UnknownQuestionTypeError(
                    f"{row.question_type!r} is not one of the six LongMemEval question types"
                )

        scored = [row for row in all_rows if row.status == "complete"]
        abstention = [row for row in scored if row.is_abstention]
        per_type = _per_type(scored)

        return Report(
            task_averaged_accuracy=_mean(stratum.accuracy for stratum in per_type),
            overall_accuracy=_rate(scored),
            abstention_accuracy=_rate(abstention),
            scored_n=len(scored),
            abstention_n=len(abstention),
            void_n=sum(1 for row in all_rows if row.status == "void"),
            error_n=sum(1 for row in all_rows if row.status == "error"),
            per_type=per_type,
            retrieval=self._retrieval(scored),
            k_values=list(self._k_values),
        )

    def _retrieval(self, scored: Sequence[ScoredRow]) -> RetrievalReport:
        """Recall at both granularities, over the rows that can carry it.

        ``_abs`` rows are dropped here and only here: they are scored for QA and
        unscoreable for retrieval, which is the whole asymmetry of REQ-190.
        """
        eligible = [row for row in scored if not row.is_abstention]
        turn_pairs = [
            (set(row.gold_turn_ids), row.retrieved_turn_ids)
            for row in eligible
            if row.gold_turn_ids
        ]
        session_pairs = [
            (set(row.gold_session_ids), _session_ranking(row.retrieved_turn_ids))
            for row in eligible
            if row.gold_session_ids
        ]
        return RetrievalReport(
            turn=[_recall_at(k, turn_pairs) for k in self._k_values],
            session=[_recall_at(k, session_pairs) for k in self._k_values],
        )


def _per_type(scored: Sequence[ScoredRow]) -> list[TypeAccuracy]:
    """One entry per type that holds at least one scored question, in canonical order.

    A type nobody ran is omitted rather than averaged in as 0.0 — a sampled
    phase would otherwise report a macro mean built from questions it skipped.
    """
    strata = []
    for question_type in QUESTION_TYPES:
        rows = [row for row in scored if row.question_type == question_type]
        if not rows:
            continue
        n_correct = sum(1 for row in rows if row.correct)
        strata.append(
            TypeAccuracy(
                question_type=question_type,
                n=len(rows),
                accuracy=n_correct / len(rows),
                wilson_95_ci=wilson_interval(n_correct, len(rows)),
                directional=len(rows) < DIRECTIONAL_MIN_N,
            )
        )
    return strata


def _recall_at(k: int, pairs: Sequence[tuple[set[str], Sequence[str]]]) -> RecallAtK:
    """Recall over (gold, ranked-retrieved) pairs at one cut-off.

    Only pairs with non-empty gold reach here: ``all()`` over an empty gold set
    is vacuously true and would report perfect ``recall_all``.
    """
    if not pairs:
        return RecallAtK(k=k, n=0, recall_any=None, recall_all=None)
    hits = [gold & set(retrieved[:k]) for gold, retrieved in pairs]
    return RecallAtK(
        k=k,
        n=len(pairs),
        recall_any=sum(1 for hit in hits if hit) / len(pairs),
        recall_all=sum(1 for hit, (gold, _) in zip(hits, pairs, strict=True) if hit == gold)
        / len(pairs),
    )


def _session_ranking(turn_ids: Sequence[str]) -> list[str]:
    """The distinct sessions of a ranked turn list, ordered by first appearance.

    A session's rank is the rank of its best turn, which is what "top k
    sessions" means when retrieval happens at turn granularity. ``rsplit`` on
    the last colon mirrors the adapter's ``<session_id>:<turn_index>`` key.
    """
    ranking: dict[str, None] = {}
    for turn_id in turn_ids:
        ranking.setdefault(turn_id.rsplit(":", 1)[0], None)
    return list(ranking)


def _rate(rows: Sequence[ScoredRow]) -> float | None:
    """The share of rows the judge passed, or ``None`` when there are none."""
    if not rows:
        return None
    return sum(1 for row in rows if row.correct) / len(rows)


def _mean(values: Iterable[float]) -> float | None:
    """The mean, or ``None`` when nothing was averaged."""
    collected = list(values)
    if not collected:
        return None
    return sum(collected) / len(collected)


__all__ = [
    "DEFAULT_K_VALUES",
    "DIRECTIONAL_MIN_N",
    "QUESTION_TYPES",
    "Z_95",
    "RecallAtK",
    "Report",
    "RetrievalReport",
    "RowStatus",
    "ScoredRow",
    "ScoringEngine",
    "TypeAccuracy",
    "UnknownQuestionTypeError",
    "WilsonInterval",
    "wilson_interval",
]
