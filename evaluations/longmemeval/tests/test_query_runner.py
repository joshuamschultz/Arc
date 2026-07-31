"""QueryRunner tests — COMP-009 / REQ-187, REQ-214, REQ-215.

Nothing here reaches a network, a provider or a filesystem: the agent is a fake
that records what it was asked, which is the whole contract the read side has.

The wall-clock assertions are the point of the file. Arc's system prompt
carries no date, so the question turn is the only anchor 133 temporal-reasoning
questions have; a harness that quietly stamped today would still produce
answers, still produce a score, and be wrong in a way no test failure reports.
``freeze_time`` moves the real clock years away from the fixture's date so the
two can never be confused for each other.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

import pytest
from freezegun import freeze_time

from evaluations.longmemeval.ingest.types import Chunk
from evaluations.longmemeval.adapter import QuestionMeta
from evaluations.longmemeval.query import (
    QUERY_SESSION_KEY,
    QueryRunner,
    attribute_recalled_chunks,
    build_query_turn,
    observe_recall,
)

QUESTION_DATE = date(2023, 5, 20)
"""The fixture's dataset date — deliberately years before any run of this suite."""


@dataclass
class _FakeResult:
    """The one field ``run_collected``'s result is read for."""

    content: str


class _FakeAgent:
    """Records every ``run_collected`` call; never calls an LLM."""

    def __init__(self, reply: str = "Paris.") -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    async def run_collected(self, input_text: str, *, session_key: str) -> Any:
        self.calls.append((input_text, session_key))
        return _FakeResult(content=self.reply)


def _meta(
    *,
    question: str = "Where did I say I was moving last month?",
    question_date: date = QUESTION_DATE,
    source: Literal["dataset", "derived"] = "dataset",
) -> QuestionMeta:
    """One question's metadata, as the adapter hands it over."""
    return QuestionMeta(
        question=question,
        question_type="temporal-reasoning",
        answer="Paris",
        answer_session_ids=["s1"],
        has_answer_turns=["s1:0"],
        question_date=question_date,
        question_date_source=source,
    )


def _chunk(text: str, *, session_idx: int = 0, chunk_idx: int = 0) -> Chunk:
    """One ingest chunk, as the chunker fed it."""
    return Chunk(text=text, session_idx=session_idx, chunk_idx=chunk_idx, turn_ids=["s1:0"])


@pytest.fixture(autouse=True)
def silent_recall(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the recall observation to "nothing injected".

    The real read is exercised against a real bound memory state further down;
    every other test is about the question turn, not about recall.
    """
    monkeypatch.setattr("evaluations.longmemeval.query.observe_recall", lambda _text: "")


# ---------------------------------------------------------------------------
# T-802 — the question turn (REQ-187, REQ-214)
# ---------------------------------------------------------------------------


async def test_query_turn_is_prefixed_with_the_dataset_question_date() -> None:
    agent = _FakeAgent()
    await QueryRunner().ask(agent, _meta(), [])

    input_text, _ = agent.calls[0]
    assert input_text.startswith("[Current date: 2023-05-20]\n")
    assert input_text.endswith("Where did I say I was moving last month?")


@freeze_time("2031-09-14")
async def test_the_prefix_ignores_the_system_clock() -> None:
    """The frozen clock is years off the fixture's date and must not appear."""
    assert date.today() == date(2031, 9, 14), "the clock patch itself must be live"
    agent = _FakeAgent()
    await QueryRunner().ask(agent, _meta(), [])

    input_text, _ = agent.calls[0]
    assert "[Current date: 2023-05-20]" in input_text
    assert "2031" not in input_text
    assert date.today().isoformat() not in input_text


@freeze_time("2031-09-14")
def test_a_derived_date_also_ignores_the_system_clock() -> None:
    """The fallback anchor is still the haystack's, not the run year."""
    assert date.today() == date(2031, 9, 14), "the clock patch itself must be live"
    turn = build_query_turn(_meta(question_date=date(2019, 1, 2), source="derived"))

    assert turn.startswith("[Current date: 2019-01-02]\n")


async def test_the_question_goes_to_a_session_key_outside_the_ingest_namespace() -> None:
    agent = _FakeAgent()
    await QueryRunner().ask(agent, _meta(), [])

    _, session_key = agent.calls[0]
    assert session_key == QUERY_SESSION_KEY
    assert not session_key.startswith("ingest:")


async def test_the_question_is_one_turn_on_one_session() -> None:
    agent = _FakeAgent()
    await QueryRunner().ask(agent, _meta(), [])

    assert len(agent.calls) == 1


async def test_the_answer_is_recorded_verbatim() -> None:
    """Whitespace, casing and markdown survive: the judge grades what was said."""
    reply = "  **Paris** — she said so\nin the second message.\t"
    agent = _FakeAgent(reply=reply)

    answer = await QueryRunner().ask(agent, _meta(), [])

    assert answer.text == reply


# ---------------------------------------------------------------------------
# T-803 — the date source travels with the answer (REQ-215)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", ["dataset", "derived"])
async def test_the_answer_carries_the_date_and_its_source(source: str) -> None:
    agent = _FakeAgent()
    meta = _meta(source="derived" if source == "derived" else "dataset")

    answer = await QueryRunner().ask(agent, meta, [])

    assert answer.question_date_used == QUESTION_DATE
    assert answer.question_date_source == source


async def test_the_row_fields_survive_serialization() -> None:
    """The result row is JSONL, so the two fields must render, not just exist."""
    answer = await QueryRunner().ask(_FakeAgent(), _meta(source="derived"), [])

    row = answer.model_dump(mode="json")
    assert row["question_date_used"] == "2023-05-20"
    assert row["question_date_source"] == "derived"


async def test_the_date_used_is_the_one_that_was_actually_asked() -> None:
    """The recorded anchor and the prompt's anchor can never drift apart."""
    agent = _FakeAgent()

    answer = await QueryRunner().ask(agent, _meta(), [])

    input_text, _ = agent.calls[0]
    assert f"[Current date: {answer.question_date_used.isoformat()}]" in input_text


def test_an_unknown_date_source_is_refused() -> None:
    """`dataset` or `derived` — a third value would be an unrecorded provenance."""
    with pytest.raises(ValueError, match="question_date_source"):
        _meta(source="guessed")  # type: ignore[arg-type]  # the point of the test


# ---------------------------------------------------------------------------
# Recall attribution
# ---------------------------------------------------------------------------


def test_a_recalled_chunk_is_attributed_by_its_ingest_coordinate() -> None:
    chunks = [_chunk("[Session date: 2023-04-01]\nuser: I am moving to Paris.")]
    recall = (
        "<memory-result source='daily-log'>\n"
        "[Session date: 2023-04-01] user: I am moving to Paris.\n"
        "</memory-result>"
    )

    assert attribute_recalled_chunks(recall, chunks) == ["0:0"]


def test_an_unrecalled_chunk_is_not_attributed() -> None:
    chunks = [
        _chunk("user: I am moving to Paris.", session_idx=0, chunk_idx=0),
        _chunk("user: my dentist is on Oak Street.", session_idx=1, chunk_idx=0),
    ]
    recall = "<memory-result>\nuser: I am moving to Paris.\n</memory-result>"

    assert attribute_recalled_chunks(recall, chunks) == ["0:0"]


def test_a_distilled_fact_is_attributed_to_no_chunk() -> None:
    """A consolidated fact is not evidence its source turn was retrieved."""
    chunks = [_chunk("user: I am moving to Paris in June.")]
    recall = "<memory-result kind='fact'>\nThe user relocated to Paris.\n</memory-result>"

    assert attribute_recalled_chunks(recall, chunks) == []


def test_an_empty_recall_attributes_nothing() -> None:
    assert attribute_recalled_chunks("", [_chunk("user: hello")]) == []


# ---------------------------------------------------------------------------
# The recall observation, against real memory runtime state
# ---------------------------------------------------------------------------


@pytest.fixture
def bound_memory_state(tmp_path: Path) -> Iterator[Any]:
    """Register and bind a real memory ``_State`` for this test's DID.

    Exercises the actual private read ``observe_recall`` performs rather than a
    restatement of it — a dead observation would otherwise report "recalled
    nothing" for every question and read as a retrieval score of zero.
    """
    from arcagent.brain.protocol import NullBrain
    from arcagent.modules.memory import _runtime
    from arcagent.modules.memory.config import MemoryConfig

    state = _runtime._State(
        config=MemoryConfig(),
        brain=NullBrain(),
        workspace=tmp_path,
        telemetry=None,
        bus=None,
        agent_did="did:arc:lme-test",
        active=True,
    )
    _runtime.bind(state)
    yield state
    _runtime.reset()


def test_observe_recall_returns_this_turn_s_injected_recall(bound_memory_state: Any) -> None:
    query = "[Current date: 2023-05-20]\nWhere did I say I was moving?"
    bound_memory_state.recall_cache[hash(query)] = "<memory-result>\nParis\n</memory-result>"

    assert observe_recall(query) == "<memory-result>\nParis\n</memory-result>"


def test_observe_recall_returns_empty_when_nothing_was_injected(bound_memory_state: Any) -> None:
    assert observe_recall("a turn no recall ran for") == ""


def test_observe_recall_refuses_an_unbound_read() -> None:
    """Loud, not empty: a failed state read must never look like an empty recall."""
    from arcagent.modules.memory import _runtime

    _runtime.reset()
    with pytest.raises(_runtime.MemoryIsolationError):
        observe_recall("no agent is bound to this task")
