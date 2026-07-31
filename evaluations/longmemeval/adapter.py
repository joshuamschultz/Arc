"""LongMemEvalAdapter (COMP-002) — one question's haystack, in the dataset's order.

The only adapter in this release (REQ-175). It maps a single LongMemEval question
into ``Session`` objects and exposes the question's own metadata; everything
downstream speaks only the COMP-001 vocabulary and never sees the raw JSON.

*Order is pinned, never sorted* (REQ-178). ``Event.ts`` is ``now()``, so
arcmemory's recency channel ranks by ingest order rather than by the haystack's
dates. Sorting here — or reversing, or normalizing — would silently change what
the benchmark measures rather than fix anything, so the dataset's own order is
what ships and what ``session_ingest_order`` records for the run manifest.

*The date anchor comes from the dataset, never from the wall clock* (REQ-214,
REQ-215). Arc's system prompt carries no date at all, so the question date is the
only anchor a relative reference like "last month" has. Stamping today would
anchor the reader to the run year instead of the haystack's, failing the 133
temporal-reasoning questions a different way, so an unresolvable date raises and
the caller voids the question instead.

Turn ids are ``<haystack_session_id>:<turn_index>``. That id is the key the
sanitize-fidelity gate computes gold overlap against (COMP-005) and the key
turn-level retrieval recall is scored on, so it must stay stable across reads and
remain addressable back to the turn it names.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from evaluations.longmemeval.ingest.types import Session, Turn
from evaluations.longmemeval.dataset import Dataset


class QuestionNotFoundError(Exception):
    """The dataset holds no question with the requested id."""


class QuestionDateError(Exception):
    """No usable date could be resolved for the question (REQ-215).

    Raised when the dataset ships neither a per-question date nor a parseable
    ``haystack_dates`` entry. Both cases are one failure to the caller: void the
    question, or fail the preflight — never substitute the wall clock.
    """


class QuestionMeta(BaseModel):
    """One question's own metadata, as the query, judge and scoring stages need it."""

    question: str
    question_type: str
    answer: str
    answer_session_ids: list[str]
    has_answer_turns: list[str]
    question_date: date
    question_date_source: Literal["dataset", "derived"]


class _RawTurn(BaseModel):
    """One haystack utterance as the dataset writes it."""

    role: str
    content: str
    has_answer: bool = False


class _RawQuestion(BaseModel):
    """One dataset entry, validated at the boundary (CON-2).

    The dataset is a third-party download feeding a memory store (LLM04), so a
    missing or wrong-shaped field must fail here rather than surface later as a
    plausible-looking bad score. Unknown fields are ignored: the two dataset files
    differ in what else they carry, and none of it is scored. ``answer_session_ids``
    defaults empty because it is meaningless for `_abs` questions (REQ-190).
    """

    question_id: str
    question_type: str
    question: str
    answer: str
    question_date: str | None = None
    answer_session_ids: list[str] = Field(default_factory=list)
    haystack_dates: list[str]
    haystack_session_ids: list[str]
    haystack_sessions: list[list[_RawTurn]]


class LongMemEvalAdapter:
    """One LongMemEval question, exposed as sessions plus question metadata."""

    def __init__(self, *, dataset: Dataset, question_id: str) -> None:
        self._question = _find(dataset.questions, question_id)

    def read(self) -> Iterator[Session]:
        """Yield the haystack's sessions in the dataset's own order (REQ-178)."""
        raw = self._question
        for session_id, stamp, turns in zip(
            raw.haystack_session_ids, raw.haystack_dates, raw.haystack_sessions, strict=True
        ):
            yield Session(
                conversation_id=session_id,
                source_date=_parse_dataset_date(stamp),
                turns=[
                    Turn(turn_id=f"{session_id}:{index}", role=turn.role, text=turn.content)
                    for index, turn in enumerate(turns)
                ],
            )

    @property
    def session_ingest_order(self) -> list[str]:
        """The pinned order, for the run manifest — without it no two runs compare."""
        return list(self._question.haystack_session_ids)

    def question_meta(self) -> QuestionMeta:
        """The question's own metadata, with its date resolved and its source named."""
        raw = self._question
        question_date, source = self._resolve_question_date()
        return QuestionMeta(
            question=raw.question,
            question_type=raw.question_type,
            answer=raw.answer,
            answer_session_ids=list(raw.answer_session_ids),
            has_answer_turns=_has_answer_turns(raw),
            question_date=question_date,
            question_date_source=source,
        )

    def _resolve_question_date(self) -> tuple[date, Literal["dataset", "derived"]]:
        """Prefer the dataset's own field; otherwise derive the latest haystack date.

        "Latest" is the newest stamp, not the last element: Oracle haystacks are
        unsorted, so a positional read would anchor the reader to an arbitrary
        session's date.
        """
        raw = self._question
        if raw.question_date:
            return _parse_dataset_date(raw.question_date), "dataset"
        if not raw.haystack_dates:
            raise QuestionDateError(
                f"question {raw.question_id!r} has neither a question_date nor any "
                "haystack_dates; it cannot be anchored and must be voided"
            )
        return max(_parse_dataset_date(stamp) for stamp in raw.haystack_dates), "derived"


def _find(questions: list[dict[str, Any]], question_id: str) -> _RawQuestion:
    """Locate and validate one question by id."""
    for raw in questions:
        if raw.get("question_id") == question_id:
            return _RawQuestion.model_validate(raw)
    raise QuestionNotFoundError(f"no question {question_id!r} in the loaded dataset")


def _parse_dataset_date(stamp: str) -> date:
    """Parse LongMemEval's ``YYYY/MM/DD (Day) HH:MM`` stamp into a date.

    An unparseable stamp raises rather than being skipped: dropping it would
    derive a plausible but wrong anchor from whatever remained.
    """
    head = stamp.strip().split(" ", 1)[0]
    try:
        return date.fromisoformat(head.replace("/", "-"))
    except ValueError as exc:
        raise QuestionDateError(f"unparseable dataset date {stamp!r}") from exc


def _has_answer_turns(raw: _RawQuestion) -> list[str]:
    """Every turn the dataset flags as gold evidence, in ingest order (REQ-181)."""
    return [
        f"{session_id}:{index}"
        for session_id, turns in zip(raw.haystack_session_ids, raw.haystack_sessions, strict=True)
        for index, turn in enumerate(turns)
        if turn.has_answer
    ]
