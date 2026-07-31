"""RED tests for COMP-002 question-date resolution — T-793.

Covers REQ-214 (the question turn is anchored to the dataset's own date, never
wall-clock time) and REQ-215 (derive the latest ``haystack_dates`` entry when the
dataset ships no per-question field, record which source was used, and fail when
neither is available).

Arc's system prompt carries no date at all, so this value is the *only* anchor a
relative reference like "last month" has. Stamping today's date would anchor 133
temporal-reasoning questions to the run year instead of the haystack's, which
fails them a different way — so every test here uses a historical fixture and the
wall clock never appears in an expected value.

Session ordering and the rest of ``QuestionMeta`` are covered by
``test_lme_adapter.py`` — T-791. This file covers only the date.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from evaluations.longmemeval.adapter import LongMemEvalAdapter, QuestionDateError
from evaluations.longmemeval.dataset import Dataset

_FIXTURES = Path(__file__).parent / "fixtures"
_ORACLE_FIXTURE = _FIXTURES / "lme_oracle_fixture.json"
_S_FIXTURE = _FIXTURES / "lme_s_fixture.json"

_ORACLE_QUESTION_ID = "fixture_oracle_temporal_reasoning_1"
_S_QUESTION_ID = "fixture_s_single_session_assistant_1"

_REVISION = "1d0f1a5c9e3b47a28f6c05d9b7e4a3128c9f6b02"

# The Oracle haystack is unsorted, so its LATEST date is neither the first nor the
# last entry. An adapter that took `haystack_dates[-1]` would derive 2023-05-18.
_ORACLE_LATEST_HAYSTACK_DATE = date(2023, 9, 1)
_ORACLE_LAST_HAYSTACK_DATE = date(2023, 5, 18)


def _question(path: Path, question_id: str) -> dict[str, Any]:
    for raw in json.loads(path.read_bytes()):
        if raw["question_id"] == question_id:
            question: dict[str, Any] = raw
            return question
    raise AssertionError(f"fixture {path.name} has no question {question_id}")


def _adapter_over(question: dict[str, Any]) -> LongMemEvalAdapter:
    """Build an adapter over one hand-edited question, bypassing the file on disk."""
    dataset = Dataset(questions=[question], sha256="0" * 64, revision=_REVISION)
    return LongMemEvalAdapter(dataset=dataset, question_id=question["question_id"])


def _oracle_question_without(*fields: str) -> dict[str, Any]:
    question = copy.deepcopy(_question(_ORACLE_FIXTURE, _ORACLE_QUESTION_ID))
    for field in fields:
        question.pop(field, None)
    return question


def test_question_date_comes_from_the_dataset_field_when_present() -> None:
    meta = _adapter_over(_question(_ORACLE_FIXTURE, _ORACLE_QUESTION_ID)).question_meta()

    assert meta.question_date == date(2023, 9, 20)
    assert meta.question_date_source == "dataset"


def test_the_s_dataset_field_resolves_the_same_way() -> None:
    meta = _adapter_over(_question(_S_FIXTURE, _S_QUESTION_ID)).question_meta()

    assert meta.question_date == date(2023, 8, 30)
    assert meta.question_date_source == "dataset"


def test_a_missing_dataset_field_derives_the_latest_haystack_date() -> None:
    """REQ-215: latest means newest, not last — the Oracle haystack is unsorted."""
    question = _oracle_question_without("question_date")

    meta = _adapter_over(question).question_meta()

    assert meta.question_date == _ORACLE_LATEST_HAYSTACK_DATE
    assert meta.question_date_source == "derived"
    assert meta.question_date != _ORACLE_LAST_HAYSTACK_DATE


def test_an_empty_dataset_field_is_treated_as_absent() -> None:
    """A blank string is no anchor; deriving beats prefixing `[Current date: ]`."""
    question = _question(_ORACLE_FIXTURE, _ORACLE_QUESTION_ID) | {"question_date": ""}

    meta = _adapter_over(question).question_meta()

    assert meta.question_date == _ORACLE_LATEST_HAYSTACK_DATE
    assert meta.question_date_source == "derived"


def test_neither_source_available_raises_rather_than_falling_back_to_today() -> None:
    """The wall clock is never an answer here — a run-year anchor fails temporal a new way."""
    question = _oracle_question_without("question_date")
    question["haystack_dates"] = []

    with pytest.raises(QuestionDateError):
        _adapter_over(question).question_meta()


def test_an_unparseable_date_raises_rather_than_being_skipped() -> None:
    """A silently dropped bad stamp would derive a plausible but wrong anchor."""
    question = _oracle_question_without("question_date")
    question["haystack_dates"][2] = "not a date"

    with pytest.raises(QuestionDateError):
        _adapter_over(question).question_meta()


def test_the_resolved_date_is_never_the_wall_clock_date() -> None:
    """REQ-214 stated as an assertion: both paths stay in the haystack's year."""
    for question in (
        _question(_ORACLE_FIXTURE, _ORACLE_QUESTION_ID),
        _oracle_question_without("question_date"),
    ):
        assert _adapter_over(question).question_meta().question_date != date.today()
