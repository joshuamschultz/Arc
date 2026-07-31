"""Tests for COMP-021 BudgetGovernor — T-817 and T-818.

Covers REQ-208 (the dry-run estimate walks the REAL chunker and multiplies by a
versioned pricing table), REQ-209 (the ceiling ABORTS at 110% of the estimate
and the running total survives a resume) and REQ-212 (five cost fields logged
per question).

Two of these tests exist because of specific failure modes rather than for
coverage. ``test_estimate_drives_every_session_through_the_production_chunker``
spies on ``TurnChunker.split`` itself: a private re-implementation of the
packing rule would pass every arithmetic assertion here while drifting from
what ingest actually sends. ``test_ceiling_raises_rather_than_only_logging``
asserts the raise, because a warning-only ceiling is precisely the control this
component exists to replace.

Nothing here touches the network or writes outside ``tmp_path``.
"""

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from evaluations.ingest import chunker as production_chunker
from evaluations.ingest.types import Chunk, Session
from evaluations.longmemeval import budget
from evaluations.longmemeval.adapter import LongMemEvalAdapter
from evaluations.longmemeval.budget import (
    CURRENT_PRICING_TABLE_VERSION,
    PRICING_TABLES,
    BudgetGovernor,
    CallProfile,
    DatasetUnavailableError,
    Estimate,
    ModelRate,
    PricingTable,
    QuestionCost,
    SpendCeilingExceeded,
    UnknownPricingError,
    estimate_dataset_file,
    estimate_run,
)
from evaluations.longmemeval.dataset import Dataset

CAP = 2000
"""``arcmemory.security.sanitize`` caps at ``max_length=2000``."""

AGENT_MODEL = "anthropic/claude-sonnet-4-5-20250929"
JUDGE_MODEL = "openai/gpt-4o-2024-08-06"

PROFILE = CallProfile(agent_model_id=AGENT_MODEL, judge_model_id=JUDGE_MODEL)


# --------------------------------------------------------------------------
# Fixture data — a two-question dataset whose sessions really do split
# --------------------------------------------------------------------------


def _turn(index: int, length: int) -> dict[str, Any]:
    """One raw haystack utterance of exactly ``length`` characters."""
    marker = f"turn-{index:03d} "
    return {"role": "user", "content": marker + "x" * (length - len(marker))}


def _question(question_id: str, *, n_sessions: int, turns_per_session: int) -> dict[str, Any]:
    """One raw dataset entry with 421-char turns, which pack three to a chunk."""
    return {
        "question_id": question_id,
        "question_type": "single-session-user",
        "question": "what did I say?",
        "answer": "something",
        "question_date": "2023/06/01 (Thu) 09:00",
        "answer_session_ids": [f"{question_id}-s0"],
        "haystack_dates": [f"2023/05/{20 + i:02d} (Sat) 10:00" for i in range(n_sessions)],
        "haystack_session_ids": [f"{question_id}-s{i}" for i in range(n_sessions)],
        "haystack_sessions": [
            [_turn(i, length=421) for i in range(turns_per_session)] for _ in range(n_sessions)
        ],
    }


def _dataset(questions: list[dict[str, Any]]) -> Dataset:
    return Dataset(questions=questions, sha256="0" * 64, revision="test-revision")


def _two_question_dataset() -> Dataset:
    return _dataset(
        [
            _question("q1", n_sessions=2, turns_per_session=15),
            _question("q2", n_sessions=1, turns_per_session=4),
        ]
    )


def _real_chunk_count(dataset: Dataset) -> tuple[int, int, list[Chunk]]:
    """Chunk the fixture with the production adapter and chunker, independently.

    Deliberately re-walks the same public path the estimate does rather than
    reading the estimate's own numbers back — otherwise a wrong chunk count
    would agree with itself.
    """
    chunks: list[Chunk] = []
    n_sessions = 0
    tool = production_chunker.TurnChunker(max_event_chars=CAP)
    for raw in dataset.questions:
        adapter = LongMemEvalAdapter(dataset=dataset, question_id=raw["question_id"])
        for index, session in enumerate(adapter.read()):
            n_sessions += 1
            chunks.extend(tool.split(session, session_idx=index))
    return n_sessions, len(chunks), chunks


# --------------------------------------------------------------------------
# T-817 — the estimate goes through the production chunker
# --------------------------------------------------------------------------


def test_budget_imports_the_production_chunker_not_a_copy() -> None:
    """REQ-208: the symbol the estimate uses IS ``evaluations.ingest.chunker``'s."""
    # Read through __dict__ because the name is an import, not part of budget's API.
    assert budget.__dict__["TurnChunker"] is production_chunker.TurnChunker


def test_estimate_drives_every_session_through_the_production_chunker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REQ-208: ``TurnChunker.split`` is called once per session, and its chunks are used."""
    # Arrange — measure first, then spy, so only the estimate's own calls are counted.
    dataset = _two_question_dataset()
    n_sessions, n_chunks, _ = _real_chunk_count(dataset)
    calls: list[int] = []
    original = production_chunker.TurnChunker.split

    def spy(self: production_chunker.TurnChunker, session: Session, *, session_idx: int) -> Any:
        produced = original(self, session, session_idx=session_idx)
        calls.append(len(produced))
        return produced

    monkeypatch.setattr(production_chunker.TurnChunker, "split", spy)

    # Act
    estimate = estimate_run(dataset, profile=PROFILE)

    # Assert
    assert len(calls) == n_sessions
    assert sum(calls) == n_chunks
    assert estimate.n_chunks == n_chunks


def test_call_count_is_derived_from_the_real_chunk_boundaries() -> None:
    """One ingest call per real chunk, plus consolidation, query and judge."""
    # Arrange
    dataset = _two_question_dataset()
    n_sessions, n_chunks, _ = _real_chunk_count(dataset)

    # Act
    estimate = estimate_run(dataset, profile=PROFILE)

    # Assert
    assert estimate.n_chunks == n_chunks
    assert estimate.n_sessions == n_sessions
    assert estimate.n_questions == 2
    assert estimate.n_calls == n_chunks + n_sessions + 2 * 2


def test_estimate_exceeds_raw_text_length_over_four() -> None:
    """The naive ``len(text)/4`` shortcut understates: prefixes and overhead repeat."""
    # Arrange
    dataset = _two_question_dataset()
    raw_chars = sum(
        len(turn["content"])
        for raw in dataset.questions
        for session in raw["haystack_sessions"]
        for turn in session
    )

    # Act
    estimate = estimate_run(dataset, profile=PROFILE)

    # Assert — the repeated date prefix and the per-call prompt overhead are real spend.
    assert estimate.tokens_in > raw_chars / 4


def test_chunk_text_including_the_repeated_date_prefix_is_counted() -> None:
    """Every chunk re-carries its session date, so every chunk pays for it."""
    # Arrange
    dataset = _two_question_dataset()
    _, _, chunks = _real_chunk_count(dataset)
    chunk_chars = sum(len(chunk.text) for chunk in chunks)
    prefix_chars = sum(len(chunk.text.split("\n")[0]) for chunk in chunks)
    chars_per_token = PRICING_TABLES[CURRENT_PRICING_TABLE_VERSION].chars_per_token

    # Act
    estimate = estimate_run(dataset, profile=PROFILE)

    # Assert
    assert prefix_chars > 0
    assert estimate.tokens_in > chunk_chars / chars_per_token


# --------------------------------------------------------------------------
# T-817 — the versioned pricing table
# --------------------------------------------------------------------------


def test_estimate_records_the_pricing_table_version_it_used() -> None:
    """REQ-208: the run manifest has to say which prices produced the ceiling."""
    # Act
    estimate = estimate_run(_two_question_dataset(), profile=PROFILE)

    # Assert
    assert estimate.pricing_table_version == CURRENT_PRICING_TABLE_VERSION
    assert estimate.pricing_table_version in PRICING_TABLES


def test_cost_tracks_the_pricing_table_rather_than_being_hardcoded() -> None:
    """Doubling every rate in the table doubles the estimated cost."""
    # Arrange
    base = PRICING_TABLES[CURRENT_PRICING_TABLE_VERSION]
    doubled = PricingTable(
        version="test-doubled",
        chars_per_token=base.chars_per_token,
        rates={
            model: ModelRate(
                input_per_1m=rate.input_per_1m * 2, output_per_1m=rate.output_per_1m * 2
            )
            for model, rate in base.rates.items()
        },
    )
    dataset = _two_question_dataset()

    # Act
    cheap = estimate_run(dataset, profile=PROFILE)
    dear = estimate_run(
        dataset,
        profile=PROFILE,
        pricing_table_version="test-doubled",
        tables={"test-doubled": doubled},
    )

    # Assert
    assert dear.cost_usd == pytest.approx(cheap.cost_usd * 2)
    assert dear.pricing_table_version == "test-doubled"


def test_unknown_pricing_table_version_raises() -> None:
    """A version nobody shipped must not silently fall back to today's prices."""
    with pytest.raises(UnknownPricingError):
        estimate_run(_two_question_dataset(), profile=PROFILE, pricing_table_version="1999-01-01")


def test_model_missing_from_the_pricing_table_raises() -> None:
    """An unpriced model would estimate as free and defeat the ceiling entirely."""
    profile = CallProfile(agent_model_id="acme/unpriced-1", judge_model_id=JUDGE_MODEL)
    with pytest.raises(UnknownPricingError):
        estimate_run(_two_question_dataset(), profile=profile)


def test_shipped_pricing_table_prices_both_the_agent_and_the_judge() -> None:
    """The default profile must be estimable without any caller configuration."""
    table = PRICING_TABLES[CURRENT_PRICING_TABLE_VERSION]
    assert AGENT_MODEL in table.rates
    assert JUDGE_MODEL in table.rates


# --------------------------------------------------------------------------
# T-817 — voids and the absent dataset
# --------------------------------------------------------------------------


def test_question_with_an_over_cap_turn_is_counted_void_not_estimated() -> None:
    """REQ-177 voids that question, so its chunks are not spend to plan for."""
    # Arrange
    doomed = _question("q-void", n_sessions=1, turns_per_session=1)
    doomed["haystack_sessions"][0][0]["content"] = "y" * (CAP + 500)
    dataset = _dataset([_question("q1", n_sessions=1, turns_per_session=4), doomed])

    # Act
    estimate = estimate_run(dataset, profile=PROFILE)

    # Assert
    assert estimate.n_voided_questions == 1
    assert estimate.n_questions == 1


def test_dry_run_on_an_absent_dataset_raises_naming_the_path(tmp_path: Path) -> None:
    """The real dataset is gitignored and undownloaded — never invent one."""
    # Arrange
    missing = tmp_path / "data" / "longmemeval_s_cleaned.json"

    # Act / Assert
    with pytest.raises(DatasetUnavailableError) as exc_info:
        estimate_dataset_file(missing, revision="test-revision", profile=PROFILE)
    assert str(missing) in str(exc_info.value)


def test_dry_run_reads_a_present_dataset_file(tmp_path: Path) -> None:
    """The happy path goes through the production loader, hash gate included."""
    # Arrange
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(_two_question_dataset().questions), encoding="utf-8")

    # Act
    estimate = estimate_dataset_file(path, revision="test-revision", profile=PROFILE)

    # Assert
    assert estimate.n_questions == 2
    assert estimate.cost_usd > 0


# --------------------------------------------------------------------------
# T-818 — the ceiling aborts
# --------------------------------------------------------------------------


def _estimate(cost_usd: float) -> Estimate:
    """A minimal Estimate standing in for a completed dry run."""
    return Estimate(
        n_calls=100,
        tokens_in=1_000,
        tokens_out=100,
        cost_usd=cost_usd,
        pricing_table_version=CURRENT_PRICING_TABLE_VERSION,
        n_questions=10,
        n_sessions=10,
        n_chunks=80,
        n_voided_questions=0,
    )


def _cost(question_id: str, cost_usd: float) -> QuestionCost:
    return QuestionCost(
        question_id=question_id,
        tokens_in=1_000,
        tokens_out=200,
        cost_usd=cost_usd,
        n_llm_calls=8,
        wall_seconds=12.5,
    )


def test_ceiling_raises_rather_than_only_logging(tmp_path: Path) -> None:
    """REQ-209: a warning is not a control. Crossing 110% must abort the run."""
    # Arrange
    governor = BudgetGovernor(estimate=_estimate(10.0), ledger_path=tmp_path / "spend.jsonl")
    governor.record(_cost("q1", 5.0))

    # Act / Assert
    with pytest.raises(SpendCeilingExceeded):
        governor.record(_cost("q2", 6.5))


def test_spend_below_the_ceiling_does_not_raise(tmp_path: Path) -> None:
    """109% is still inside the ceiling — the abort is not a hair trigger."""
    # Arrange
    governor = BudgetGovernor(estimate=_estimate(10.0), ledger_path=tmp_path / "spend.jsonl")

    # Act
    governor.record(_cost("q1", 10.9))

    # Assert
    assert governor.spent_usd == pytest.approx(10.9)


def test_ceiling_is_exactly_110_percent_of_the_estimate(tmp_path: Path) -> None:
    """REQ-209 says 'reaches 110%', so the boundary itself aborts."""
    # Arrange
    governor = BudgetGovernor(estimate=_estimate(10.0), ledger_path=tmp_path / "spend.jsonl")

    # Act / Assert
    assert governor.ceiling_usd == pytest.approx(11.0)
    with pytest.raises(SpendCeilingExceeded):
        governor.record(_cost("q1", 11.0))


def test_the_row_that_breaks_the_ceiling_is_persisted_before_the_raise(tmp_path: Path) -> None:
    """Losing that row on abort would let a resume restart under the ceiling."""
    # Arrange
    ledger = tmp_path / "spend.jsonl"
    governor = BudgetGovernor(estimate=_estimate(10.0), ledger_path=ledger)

    # Act
    with pytest.raises(SpendCeilingExceeded):
        governor.record(_cost("q-breaker", 12.0))

    # Assert
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert [row["question_id"] for row in rows] == ["q-breaker"]


# --------------------------------------------------------------------------
# T-818 — the running total survives a resume
# --------------------------------------------------------------------------


def test_running_total_persists_across_a_resume(tmp_path: Path) -> None:
    """REQ-209: a fresh governor on the same ledger resumes the same total."""
    # Arrange
    ledger = tmp_path / "spend.jsonl"
    first = BudgetGovernor(estimate=_estimate(100.0), ledger_path=ledger)
    first.record(_cost("q1", 3.0))
    first.record(_cost("q2", 4.0))

    # Act — a SIGKILL and a resume look exactly like a second construction.
    resumed = BudgetGovernor(estimate=_estimate(100.0), ledger_path=ledger)

    # Assert
    assert resumed.spent_usd == pytest.approx(7.0)


def test_resume_over_the_ceiling_aborts_at_construction(tmp_path: Path) -> None:
    """A resume must not get a fresh budget just because the process restarted."""
    # Arrange
    ledger = tmp_path / "spend.jsonl"
    first = BudgetGovernor(estimate=_estimate(10.0), ledger_path=ledger)
    with pytest.raises(SpendCeilingExceeded):
        first.record(_cost("q1", 11.5))

    # Act / Assert
    with pytest.raises(SpendCeilingExceeded):
        BudgetGovernor(estimate=_estimate(10.0), ledger_path=ledger)


def test_truncated_final_line_is_tolerated_on_resume(tmp_path: Path) -> None:
    """A half-written final line is the normal SIGKILL signature, not corruption."""
    # Arrange
    ledger = tmp_path / "spend.jsonl"
    governor = BudgetGovernor(estimate=_estimate(100.0), ledger_path=ledger)
    governor.record(_cost("q1", 3.0))
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write('{"question_id": "q2", "cost_us')

    # Act
    resumed = BudgetGovernor(estimate=_estimate(100.0), ledger_path=ledger)

    # Assert
    assert resumed.spent_usd == pytest.approx(3.0)


def test_a_missing_ledger_starts_at_zero(tmp_path: Path) -> None:
    """The first run of a phase has nothing to resume."""
    governor = BudgetGovernor(estimate=_estimate(10.0), ledger_path=tmp_path / "spend.jsonl")
    assert governor.spent_usd == 0.0


# --------------------------------------------------------------------------
# T-818 / REQ-212 — per-question cost telemetry
# --------------------------------------------------------------------------


def test_every_persisted_row_carries_all_five_cost_fields(tmp_path: Path) -> None:
    """REQ-212: the median-cost diff against the prior run reads these rows."""
    # Arrange
    ledger = tmp_path / "spend.jsonl"
    governor = BudgetGovernor(estimate=_estimate(100.0), ledger_path=ledger)

    # Act
    governor.record(_cost("q1", 1.25))

    # Assert
    row = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
    assert row["question_id"] == "q1"
    assert row["tokens_in"] == 1_000
    assert row["tokens_out"] == 200
    assert row["cost_usd"] == pytest.approx(1.25)
    assert row["n_llm_calls"] == 8
    assert row["wall_seconds"] == pytest.approx(12.5)


def test_each_question_is_logged_with_all_five_cost_fields(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """REQ-212 asks for a log line as well as a row — an operator watches the run."""
    # Arrange
    governor = BudgetGovernor(estimate=_estimate(100.0), ledger_path=tmp_path / "spend.jsonl")

    # Act
    with caplog.at_level(logging.INFO, logger=budget.LOGGER_NAME):
        governor.record(_cost("q1", 1.25))

    # Assert
    message = caplog.text
    for field in ("tokens_in", "tokens_out", "cost_usd", "n_llm_calls", "wall_seconds"):
        assert field in message


def test_recorded_questions_accumulate_in_order(tmp_path: Path) -> None:
    """The ledger is append-only, so the rows stay in completion order."""
    # Arrange
    ledger = tmp_path / "spend.jsonl"
    governor = BudgetGovernor(estimate=_estimate(100.0), ledger_path=ledger)

    # Act
    for index in range(3):
        governor.record(_cost(f"q{index}", 1.0))

    # Assert
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert [row["question_id"] for row in rows] == ["q0", "q1", "q2"]
    assert governor.spent_usd == pytest.approx(3.0)
