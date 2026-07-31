"""The ScoringEngine — COMP-012 / REQ-190, REQ-191, REQ-192, REQ-193.

Four claims are asserted here, and each one is a number someone could publish:

* the three accuracy figures are three *different* numbers — macro over the six
  types, micro over every scored question, and abstention on its own — and the
  tests are built so a macro/micro conflation shows up as a wrong value rather
  than as an equal one (REQ-192);
* `_abs` rows fold into their base type for QA accuracy and vanish from
  retrieval, because `answer_session_ids` is meaningless for them (REQ-190);
* recall is reported as `any@k` and `all@k` at both turn and session level with
  every k named in the output, since "recall" without the qualifier compares to
  nothing (REQ-192);
* the confidence interval is Wilson, checked against published values and
  separated from the normal approximation it is easy to ship by mistake
  (REQ-193).

Nothing here touches the filesystem, the network or an agent: scoring is pure
arithmetic over rows, and the tests keep it that way.
"""

from __future__ import annotations

import math

import pytest

from evaluations.longmemeval.judge import Verdict
from evaluations.longmemeval.scoring import (
    DEFAULT_K_VALUES,
    DIRECTIONAL_MIN_N,
    QUESTION_TYPES,
    Z_95,
    RowStatus,
    ScoredRow,
    ScoringEngine,
    UnknownQuestionTypeError,
    wilson_interval,
)


def _verdict(correct: bool) -> Verdict:
    """A verdict shaped like the judge's, with only `correct` load-bearing here."""
    return Verdict(
        correct=correct,
        raw_response="yes" if correct else "no",
        prompt_used="<prompt>",
        judge_model_id="gpt-4o-2024-08-06",
    )


def _row(
    question_id: str,
    question_type: str = "single-session-user",
    *,
    correct: bool = True,
    status: RowStatus = "complete",
    gold_turn_ids: list[str] | None = None,
    gold_session_ids: list[str] | None = None,
    retrieved_turn_ids: list[str] | None = None,
) -> ScoredRow:
    """One row, defaulting to the boring case so each test states only its variable."""
    return ScoredRow(
        question_id=question_id,
        question_type=question_type,
        status=status,
        verdict=_verdict(correct) if status == "complete" else None,
        gold_turn_ids=gold_turn_ids or [],
        gold_session_ids=gold_session_ids or [],
        retrieved_turn_ids=retrieved_turn_ids or [],
    )


def _rows(question_type: str, n_correct: int, n_wrong: int, *, tag: str = "") -> list[ScoredRow]:
    """`n_correct` + `n_wrong` complete rows of one type, with unique ids."""
    return [
        _row(f"{question_type}{tag}-{i}", question_type, correct=i < n_correct)
        for i in range(n_correct + n_wrong)
    ]


# ---------------------------------------------------------------------------
# Wilson 95% confidence interval (REQ-193)
# ---------------------------------------------------------------------------


REFERENCE_WILSON_95 = {
    # Cross-checked against statsmodels
    # `proportion_confint(k, n, alpha=0.05, method="wilson")`, which is an
    # independent implementation of the same interval. Pinned to ten decimals
    # so a drift into the normal approximation, or a swapped z, cannot pass.
    (50, 100): (0.4038315304, 0.5961684696),
    (0, 10): (0.0000000000, 0.2775327999),
    (1, 1): (0.2065493144, 1.0000000000),
    (10, 13): (0.4974362405, 0.9182047128),
    (3, 4): (0.3006418426, 0.9544127392),
    (20, 30): (0.4878005164, 0.8076950192),
}


@pytest.mark.parametrize(("successes", "n"), list(REFERENCE_WILSON_95))
def test_wilson_interval_matches_an_independent_implementation(successes: int, n: int) -> None:
    """Every bound agrees with statsmodels to ten decimals, including both clamps.

    ``0/10`` and ``1/1`` are in the table on purpose: they are where the normal
    approximation degenerates to a zero-width interval and where a missing
    clamp reports a bound outside [0, 1].
    """
    expected_lower, expected_upper = REFERENCE_WILSON_95[successes, n]

    interval = wilson_interval(successes, n)

    assert interval.lower == pytest.approx(expected_lower, abs=1e-10)
    assert interval.upper == pytest.approx(expected_upper, abs=1e-10)


def test_wilson_interval_matches_the_spec_example_at_n_13() -> None:
    """The PRD's own example: n=13 at ~77% observed spans roughly 50% to 92%."""
    interval = wilson_interval(10, 13)
    assert round(interval.lower, 2) == 0.50
    assert round(interval.upper, 2) == 0.92


def test_wilson_interval_is_not_the_normal_approximation() -> None:
    """Wilson is asymmetric about p-hat; the normal approximation is symmetric by construction.

    This is the test that catches the easy mistake, since both formulas produce
    a plausible-looking interval and only one of them is right at small n.
    """
    successes, n = 10, 13
    p_hat = successes / n
    half_normal = Z_95 * math.sqrt(p_hat * (1 - p_hat) / n)

    interval = wilson_interval(successes, n)

    assert interval.lower < p_hat - half_normal - 0.03
    assert interval.upper < p_hat + half_normal
    below = p_hat - interval.lower
    above = interval.upper - p_hat
    assert below > above  # asymmetric: Wilson pulls the interval toward 0.5


def test_wilson_interval_rejects_an_empty_stratum() -> None:
    """A rate with no denominator has no interval; producing one would invent data."""
    with pytest.raises(ValueError, match="n must be positive"):
        wilson_interval(0, 0)


def test_wilson_interval_rejects_impossible_counts() -> None:
    with pytest.raises(ValueError, match="successes"):
        wilson_interval(11, 10)
    with pytest.raises(ValueError, match="successes"):
        wilson_interval(-1, 10)


# ---------------------------------------------------------------------------
# The three accuracy numbers (REQ-192)
# ---------------------------------------------------------------------------


def test_task_averaged_is_macro_and_overall_is_micro() -> None:
    """Unbalanced strata: macro is 0.5 and micro is 10/12 — conflating them shows up."""
    rows = _rows("single-session-user", 10, 0) + _rows("multi-session", 0, 2)

    report = ScoringEngine().score(rows)

    assert report.task_averaged_accuracy == pytest.approx(0.5)
    assert report.overall_accuracy == pytest.approx(10 / 12)
    assert report.scored_n == 12


def test_abstention_rows_fold_into_their_base_type_and_into_the_micro_mean() -> None:
    """`_abs` is a cross-tag on the question id, not a seventh type (REQ-190)."""
    rows = [
        _row("ku-1", "knowledge-update", correct=True),
        _row("ku-2_abs", "knowledge-update", correct=False),
    ]

    report = ScoringEngine().score(rows)

    knowledge_update = next(t for t in report.per_type if t.question_type == "knowledge-update")
    assert knowledge_update.n == 2
    assert knowledge_update.accuracy == pytest.approx(0.5)
    assert report.overall_accuracy == pytest.approx(0.5)
    assert [t.question_type for t in report.per_type] == ["knowledge-update"]


def test_abstention_accuracy_is_reported_separately_over_abs_rows_only() -> None:
    rows = [
        _row("mu-1", "multi-session", correct=False),
        _row("mu-2_abs", "multi-session", correct=True),
        _row("mu-3_abs", "multi-session", correct=True),
    ]

    report = ScoringEngine().score(rows)

    assert report.abstention_accuracy == pytest.approx(1.0)
    assert report.abstention_n == 2
    assert report.overall_accuracy == pytest.approx(2 / 3)


def test_abstention_accuracy_is_none_when_no_abstention_rows_were_scored() -> None:
    """A rate with a zero denominator is absent, never 0.0 — 0.0 reads as a result."""
    report = ScoringEngine().score(_rows("temporal-reasoning", 1, 0))

    assert report.abstention_accuracy is None
    assert report.abstention_n == 0


def test_void_and_error_rows_are_excluded_from_every_accuracy_and_counted() -> None:
    rows = [
        _row("ok-1", "single-session-user", correct=True),
        _row("void-1", "single-session-user", status="void"),
        _row("void-2_abs", "single-session-user", status="void"),
        _row("err-1", "single-session-user", status="error"),
    ]

    report = ScoringEngine().score(rows)

    assert report.scored_n == 1
    assert report.void_n == 2
    assert report.error_n == 1
    assert report.overall_accuracy == pytest.approx(1.0)
    assert report.task_averaged_accuracy == pytest.approx(1.0)
    assert report.abstention_accuracy is None  # the only `_abs` row was void
    assert next(t for t in report.per_type if t.question_type == "single-session-user").n == 1


def test_accuracy_figures_are_absent_when_nothing_was_scored() -> None:
    report = ScoringEngine().score([_row("void-1", status="void")])

    assert report.task_averaged_accuracy is None
    assert report.overall_accuracy is None
    assert report.per_type == []


def test_every_one_of_the_six_types_is_reported_when_present() -> None:
    rows = [row for qtype in QUESTION_TYPES for row in _rows(qtype, 1, 1)]

    report = ScoringEngine().score(rows)

    assert [t.question_type for t in report.per_type] == list(QUESTION_TYPES)
    assert report.task_averaged_accuracy == pytest.approx(0.5)
    assert len(QUESTION_TYPES) == 6


def test_an_unknown_question_type_is_refused_rather_than_averaged_in() -> None:
    """A seventh type would silently skew the macro mean, so it fails loudly instead."""
    with pytest.raises(UnknownQuestionTypeError, match="single-session-user_abs"):
        ScoringEngine().score([_row("q-1", "single-session-user_abs")])


def test_a_complete_row_without_a_verdict_is_refused() -> None:
    """Counting a missing verdict as incorrect would quietly understate accuracy."""
    with pytest.raises(ValueError, match="verdict"):
        ScoredRow(
            question_id="q-1",
            question_type="multi-session",
            status="complete",
            verdict=None,
        )


# ---------------------------------------------------------------------------
# Directional flagging (REQ-193)
# ---------------------------------------------------------------------------


def test_a_stratum_below_thirty_is_flagged_directional() -> None:
    report = ScoringEngine().score(_rows("temporal-reasoning", 10, 3))

    stratum = report.per_type[0]
    assert stratum.n == 13
    assert stratum.n < DIRECTIONAL_MIN_N
    assert stratum.directional is True
    assert stratum.accuracy == pytest.approx(10 / 13)
    assert stratum.wilson_95_ci.lower == pytest.approx(0.4974, abs=5e-4)
    assert stratum.wilson_95_ci.upper == pytest.approx(0.9182, abs=5e-4)


def test_a_stratum_at_thirty_is_not_flagged_directional() -> None:
    report = ScoringEngine().score(_rows("multi-session", 20, 10))

    stratum = report.per_type[0]
    assert stratum.n == 30
    assert stratum.directional is False


def test_every_per_type_row_carries_a_wilson_interval_around_its_accuracy() -> None:
    rows = [row for qtype in QUESTION_TYPES for row in _rows(qtype, 3, 1)]

    report = ScoringEngine().score(rows)

    assert len(report.per_type) == 6
    for stratum in report.per_type:
        assert stratum.wilson_95_ci.lower <= stratum.accuracy <= stratum.wilson_95_ci.upper


# ---------------------------------------------------------------------------
# Retrieval recall (REQ-192)
# ---------------------------------------------------------------------------


def test_turn_level_recall_any_and_all_at_each_k() -> None:
    """One row, gold turns at ranks 2 and 4: any@2 hits, all@ nothing until k=4."""
    row = _row(
        "q-1",
        gold_turn_ids=["s1:0", "s1:1"],
        gold_session_ids=["s1"],
        retrieved_turn_ids=["s2:0", "s1:0", "s3:0", "s1:1"],
    )

    report = ScoringEngine(k_values=(1, 2, 4)).score([row])
    by_k = {entry.k: entry for entry in report.retrieval.turn}

    assert by_k[1].recall_any == pytest.approx(0.0)
    assert by_k[1].recall_all == pytest.approx(0.0)
    assert by_k[2].recall_any == pytest.approx(1.0)
    assert by_k[2].recall_all == pytest.approx(0.0)
    assert by_k[4].recall_any == pytest.approx(1.0)
    assert by_k[4].recall_all == pytest.approx(1.0)
    assert by_k[4].n == 1


def test_session_level_recall_ranks_sessions_by_first_retrieved_turn() -> None:
    """Sessions are the deduplicated prefixes of the ranked turns, in first-hit order."""
    row = _row(
        "q-1",
        gold_turn_ids=["s1:1"],
        gold_session_ids=["s1"],
        retrieved_turn_ids=["s2:0", "s2:1", "s1:0", "s1:1"],
    )

    report = ScoringEngine(k_values=(1, 2)).score([row])
    by_k = {entry.k: entry for entry in report.retrieval.session}

    assert by_k[1].recall_any == pytest.approx(0.0)  # top session is s2
    assert by_k[2].recall_any == pytest.approx(1.0)  # s1 is the second distinct session
    assert by_k[2].recall_all == pytest.approx(1.0)


def test_abstention_rows_are_excluded_from_retrieval_entirely() -> None:
    """`answer_session_ids` is meaningless for `_abs`, so it scores no recall (REQ-190)."""
    rows = [
        _row(
            "q-1",
            gold_turn_ids=["s1:0"],
            gold_session_ids=["s1"],
            retrieved_turn_ids=["s1:0"],
        ),
        _row(
            "q-2_abs",
            gold_turn_ids=["s9:0"],
            gold_session_ids=["s9"],
            retrieved_turn_ids=["s8:0"],
        ),
    ]

    report = ScoringEngine(k_values=(5,)).score(rows)

    assert report.retrieval.turn[0].n == 1
    assert report.retrieval.session[0].n == 1
    assert report.retrieval.turn[0].recall_any == pytest.approx(1.0)


def test_rows_without_gold_evidence_are_excluded_rather_than_vacuously_perfect() -> None:
    """`all()` over an empty gold set is True, which would inflate recall_all to 1.0."""
    rows = [
        _row("q-1", gold_turn_ids=[], gold_session_ids=[], retrieved_turn_ids=["s1:0"]),
        _row("q-2", gold_turn_ids=["s2:0"], gold_session_ids=["s2"], retrieved_turn_ids=["s3:0"]),
    ]

    report = ScoringEngine(k_values=(5,)).score(rows)

    assert report.retrieval.turn[0].n == 1
    assert report.retrieval.turn[0].recall_all == pytest.approx(0.0)


def test_void_rows_score_no_retrieval() -> None:
    rows = [
        _row(
            "void-1",
            status="void",
            gold_turn_ids=["s1:0"],
            gold_session_ids=["s1"],
            retrieved_turn_ids=["s1:0"],
        )
    ]

    report = ScoringEngine(k_values=(5,)).score(rows)

    assert report.retrieval.turn[0].n == 0
    assert report.retrieval.turn[0].recall_any is None
    assert report.retrieval.turn[0].recall_all is None


def test_every_k_is_stated_in_the_output() -> None:
    """ "recall@k" with the k left implicit is not comparable to anything (REQ-192)."""
    report = ScoringEngine().score(_rows("multi-session", 1, 0))

    assert report.k_values == list(DEFAULT_K_VALUES)
    assert [entry.k for entry in report.retrieval.turn] == list(DEFAULT_K_VALUES)
    assert [entry.k for entry in report.retrieval.session] == list(DEFAULT_K_VALUES)


def test_k_values_must_be_positive_and_ordered_without_duplicates() -> None:
    with pytest.raises(ValueError, match="k values"):
        ScoringEngine(k_values=(5, 1))
    with pytest.raises(ValueError, match="k values"):
        ScoringEngine(k_values=(1, 1))
    with pytest.raises(ValueError, match="k values"):
        ScoringEngine(k_values=(0,))
    with pytest.raises(ValueError, match="k values"):
        ScoringEngine(k_values=())


# ---------------------------------------------------------------------------
# Purity
# ---------------------------------------------------------------------------


def test_scoring_is_deterministic_and_does_not_mutate_its_input() -> None:
    rows = [
        *_rows("single-session-user", 3, 1),
        _row(
            "q-r",
            "multi-session",
            gold_turn_ids=["s1:0"],
            gold_session_ids=["s1"],
            retrieved_turn_ids=["s1:0"],
        ),
    ]
    before = [row.model_dump() for row in rows]
    engine = ScoringEngine()

    first = engine.score(rows)
    second = engine.score(rows)

    assert first == second
    assert [row.model_dump() for row in rows] == before
