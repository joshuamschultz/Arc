"""RED tests for COMP-002 LongMemEvalAdapter — T-791.

Covers REQ-175 (the longmemeval adapter is the only adapter in this release) and
REQ-178 (the pinned session ingest order is recorded for the run manifest).

The contract these tests pin, per SDD COMP-002::

    adapter = LongMemEvalAdapter(dataset=dataset, question_id="...")
    adapter.read()                  # -> Iterator[Session], dataset order, never sorted
    adapter.session_ingest_order    # -> list[str], the same order, for the manifest
    adapter.question_meta()         # -> QuestionMeta

and the turn-id layout::

    "<haystack_session_id>:<turn_index>"

Turn ids are the key the sanitize-fidelity gate computes gold overlap against
(COMP-005 takes `gold_turn_ids: set[str]`) and the key turn-level retrieval
recall is scored on, so they have to be stable across reads and addressable back
to the turn they name.

Two fixtures, because the two dataset files have different shapes: an Oracle
question whose haystack is UNSORTED by date, and an S question whose haystack is
timestamp-sorted. The Oracle one is the load-bearing case — its dataset order and
its date-sorted order are observably different, so an adapter that sorts (or
reverses, or otherwise normalizes) fails. That matters because `Event.ts` is
`now()`, which makes arcmemory's recency channel rank by ingest order: reordering
here silently changes what the benchmark measures.

Question-date resolution is deliberately NOT tested here — that is T-793.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

from evaluations.longmemeval.adapter import LongMemEvalAdapter, QuestionMeta
from evaluations.longmemeval.dataset import Dataset, load_dataset
from evaluations.longmemeval.ingest.adapter import SourceAdapter
from evaluations.longmemeval.ingest.types import Session

_FIXTURES = Path(__file__).parent / "fixtures"

# Hand-written LongMemEval-shaped fixtures. Never the real dataset — that lives
# in gitignored evaluations/longmemeval/data/ and is far too large to commit.
_ORACLE_FIXTURE = _FIXTURES / "lme_oracle_fixture.json"
_S_FIXTURE = _FIXTURES / "lme_s_fixture.json"

_ORACLE_QUESTION_ID = "fixture_oracle_temporal_reasoning_1"
_S_QUESTION_ID = "fixture_s_single_session_assistant_1"

_REVISION = "1d0f1a5c9e3b47a28f6c05d9b7e4a3128c9f6b02"

# The Oracle haystack in the file's own order. Date-sorted it would be
# [orc_s1_sourdough, orc_s3_dentist, orc_s0_bike, answer_orc_s2_gold] — a
# different first element, a different last element, and not the reverse either.
_ORACLE_DATASET_ORDER = [
    "orc_s0_bike",
    "orc_s1_sourdough",
    "answer_orc_s2_gold",
    "orc_s3_dentist",
]
_S_DATASET_ORDER = ["s_h0_recipes", "answer_s_h1_gold", "s_h2_travel"]


def _dataset(path: Path) -> Dataset:
    return load_dataset(path, revision=_REVISION)


def _question(path: Path, question_id: str) -> dict[str, Any]:
    for raw in _dataset(path).questions:
        if raw["question_id"] == question_id:
            return raw
    raise AssertionError(f"fixture {path.name} has no question {question_id}")


def _adapter(path: Path, question_id: str) -> LongMemEvalAdapter:
    return LongMemEvalAdapter(dataset=_dataset(path), question_id=question_id)


def _oracle() -> LongMemEvalAdapter:
    return _adapter(_ORACLE_FIXTURE, _ORACLE_QUESTION_ID)


def _s() -> LongMemEvalAdapter:
    return _adapter(_S_FIXTURE, _S_QUESTION_ID)


def _date_sorted_session_ids(path: Path, question_id: str) -> list[str]:
    """The order a date-sorting adapter would produce — the wrong answer, computed."""
    raw = _question(path, question_id)
    dates: list[str] = raw["haystack_dates"]
    ids: list[str] = raw["haystack_session_ids"]
    # "YYYY/MM/DD (Day) HH:MM" sorts lexicographically in date order.
    return [session_id for _, session_id in sorted(zip(dates, ids, strict=True))]


def test_adapter_satisfies_the_source_adapter_protocol() -> None:
    """The seam is asserted before a run, not discovered missing mid-ingest."""
    assert isinstance(_oracle(), SourceAdapter)


def test_read_returns_an_iterator_of_sessions() -> None:
    sessions = _oracle().read()

    assert isinstance(sessions, Iterator)
    assert all(isinstance(session, Session) for session in sessions)


def test_oracle_sessions_yield_in_the_datasets_own_order() -> None:
    assert [s.conversation_id for s in _oracle().read()] == _ORACLE_DATASET_ORDER


def test_oracle_order_is_not_the_date_sorted_order() -> None:
    """The anti-sort proof: recency ranks by ingest order, so sorting rewrites the score."""
    sorted_order = _date_sorted_session_ids(_ORACLE_FIXTURE, _ORACLE_QUESTION_ID)
    assert sorted_order != _ORACLE_DATASET_ORDER, "fixture no longer distinguishes the two orders"

    read_order = [s.conversation_id for s in _oracle().read()]
    assert read_order != sorted_order
    assert read_order != list(reversed(sorted_order))
    assert read_order != list(reversed(_ORACLE_DATASET_ORDER))


def test_s_sessions_yield_in_dataset_order_which_happens_to_be_timestamp_sorted() -> None:
    """S is already timestamp-sorted, so 'correct' here proves nothing on its own."""
    assert _date_sorted_session_ids(_S_FIXTURE, _S_QUESTION_ID) == _S_DATASET_ORDER
    assert [s.conversation_id for s in _s().read()] == _S_DATASET_ORDER


def test_session_ingest_order_is_exposed_for_the_run_manifest() -> None:
    """REQ-178: without the recorded order no two runs are comparable."""
    adapter = _oracle()

    assert adapter.session_ingest_order == _ORACLE_DATASET_ORDER
    assert adapter.session_ingest_order == [s.conversation_id for s in adapter.read()]


def test_sessions_carry_the_haystack_date_and_the_turn_transcript() -> None:
    sessions = list(_oracle().read())

    assert [s.source_date for s in sessions] == [
        date(2023, 6, 14),
        date(2023, 4, 2),
        date(2023, 9, 1),
        date(2023, 5, 18),
    ]

    gold = sessions[2]
    assert [turn.role for turn in gold.turns] == ["user", "assistant", "user", "assistant"]
    assert gold.turns[0].text == "I bought a 35mm prime lens the week before the Big Sur trip."


def test_turn_ids_are_unique_stable_and_addressable() -> None:
    """`turn_ids` is the gold-overlap key the fidelity gate scores on (COMP-005)."""
    adapter = _oracle()

    first = {turn.turn_id: turn.text for s in adapter.read() for turn in s.turns}
    second = {turn.turn_id: turn.text for s in adapter.read() for turn in s.turns}

    assert first == second, "turn ids must not change between reads of the same question"
    assert len(first) == 10, "every turn in the haystack needs its own id"
    assert first["answer_orc_s2_gold:0"] == (
        "I bought a 35mm prime lens the week before the Big Sur trip."
    )
    assert first["orc_s0_bike:1"].startswith("That usually means the cable has stretched.")


def test_question_meta_carries_the_questions_own_metadata() -> None:
    meta = _oracle().question_meta()

    assert isinstance(meta, QuestionMeta)
    assert meta.question == "Which lens did I say I bought right before the Big Sur trip?"
    assert meta.question_type == "temporal-reasoning"
    assert meta.answer == "A 35mm prime lens"
    assert meta.answer_session_ids == ["answer_orc_s2_gold"]


def test_question_meta_has_answer_turns_name_every_flagged_turn() -> None:
    """REQ-181's void decision reads these ids, so a missed flag silently un-voids a question."""
    adapter = _oracle()
    meta = adapter.question_meta()

    assert meta.has_answer_turns == ["answer_orc_s2_gold:0", "answer_orc_s2_gold:2"]

    by_id = {turn.turn_id: turn for s in adapter.read() for turn in s.turns}
    assert set(meta.has_answer_turns) <= set(by_id), "a gold turn id must address a real turn"
    assert all("35mm prime" in by_id[turn_id].text for turn_id in meta.has_answer_turns)


def test_question_meta_flags_an_assistant_turn_when_the_dataset_does() -> None:
    """Gold evidence is not always the user's turn — S flags the assistant's answer."""
    meta = _s().question_meta()

    assert meta.question_type == "single-session-assistant"
    assert meta.answer_session_ids == ["answer_s_h1_gold"]
    assert meta.has_answer_turns == ["answer_s_h1_gold:1"]
