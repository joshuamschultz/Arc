"""Contract tests for the ingest boundary types and the SourceAdapter seam — RED.

COMP-001 / REQ-174: the harness exposes exactly one source-adapter contract — a
single ``read()`` yielding ``Session`` objects of ordered ``Turn`` objects, each
session carrying a source date and a conversation id — so that adding a new
corpus adds exactly one module and requires no edit to the chunker, the agent
driver, or the consolidation waiter.

Two things are under test and nothing else:

1. ``Session`` / ``Turn`` / ``Chunk`` are Pydantic models that validate at the
   boundary (CON-2) — required fields are enforced and wrong types raise
   ``ValidationError`` rather than flowing into the chunker as junk.
2. A *second* corpus adapter, written here from scratch, satisfies the
   ``SourceAdapter`` protocol. It is defined inside the test and imports nothing
   from ``evaluations.longmemeval`` — that absence is the whole point of the
   seam, and it is what keeps ``evaluations/longmemeval/ingest/`` source-agnostic.

``evaluations.longmemeval.ingest.types`` and
``evaluations.longmemeval.ingest.adapter`` do not exist yet.
Following the convention in ``packages/arcstore/tests/unit/test_tasks.py``, every
import below is local to its test rather than module-level, so the missing module
surfaces as one failure per test instead of a single collection error masking the
rest.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError


def _turn_kwargs() -> dict[str, Any]:
    return {"turn_id": "s0:t0", "role": "user", "text": "I switched to a standing desk."}


def _chunk_kwargs() -> dict[str, Any]:
    return {
        "text": "[Session date: 2023-05-20]\nI switched to a standing desk.",
        "session_idx": 0,
        "chunk_idx": 0,
        "turn_ids": ["s0:t0"],
    }


# --------------------------------------------------------------------------- Turn


def test_turn_is_a_pydantic_model_carrying_role_text_and_turn_id() -> None:
    from evaluations.longmemeval.ingest.types import Turn

    turn = Turn(**_turn_kwargs())

    assert issubclass(Turn, BaseModel)
    assert turn.turn_id == "s0:t0"
    assert turn.role == "user"
    assert turn.text == "I switched to a standing desk."


@pytest.mark.parametrize("missing", ["turn_id", "role", "text"])
def test_turn_requires_every_field(missing: str) -> None:
    from evaluations.longmemeval.ingest.types import Turn

    kwargs = _turn_kwargs()
    del kwargs[missing]

    with pytest.raises(ValidationError):
        Turn(**kwargs)


@pytest.mark.parametrize("field", ["turn_id", "role", "text"])
def test_turn_rejects_a_non_string_field(field: str) -> None:
    from evaluations.longmemeval.ingest.types import Turn

    kwargs = _turn_kwargs()
    kwargs[field] = 17

    with pytest.raises(ValidationError):
        Turn(**kwargs)


# ------------------------------------------------------------------------ Session


def test_session_is_a_pydantic_model_carrying_id_source_date_and_turns() -> None:
    from datetime import date

    from evaluations.longmemeval.ingest.types import Session, Turn

    session = Session(
        conversation_id="conv-1",
        source_date=date(2023, 5, 20),
        turns=[Turn(**_turn_kwargs())],
    )

    assert issubclass(Session, BaseModel)
    assert session.conversation_id == "conv-1"
    assert session.source_date == date(2023, 5, 20)
    assert [turn.turn_id for turn in session.turns] == ["s0:t0"]


def test_session_preserves_turn_order() -> None:
    """Ingest order is what the recency channel ranks by (REQ-178) — never reorder."""
    from datetime import date

    from evaluations.longmemeval.ingest.types import Session, Turn

    turns = [
        Turn(turn_id=f"s0:t{index}", role="user", text=f"message {index}") for index in range(5)
    ]

    session = Session(conversation_id="conv-1", source_date=date(2023, 5, 20), turns=turns)

    assert [turn.turn_id for turn in session.turns] == [f"s0:t{index}" for index in range(5)]


@pytest.mark.parametrize("missing", ["conversation_id", "source_date", "turns"])
def test_session_requires_every_field(missing: str) -> None:
    from datetime import date

    from evaluations.longmemeval.ingest.types import Session, Turn

    kwargs: dict[str, Any] = {
        "conversation_id": "conv-1",
        "source_date": date(2023, 5, 20),
        "turns": [Turn(**_turn_kwargs())],
    }
    del kwargs[missing]

    with pytest.raises(ValidationError):
        Session(**kwargs)


def test_session_rejects_an_unparseable_source_date() -> None:
    """The date is stamped onto every chunk (REQ-178); a junk date must not reach it."""
    from evaluations.longmemeval.ingest.types import Session, Turn

    kwargs: dict[str, Any] = {
        "conversation_id": "conv-1",
        "source_date": "last tuesday",
        "turns": [Turn(**_turn_kwargs())],
    }

    with pytest.raises(ValidationError):
        Session(**kwargs)


def test_session_rejects_a_non_turn_member_of_turns() -> None:
    from datetime import date

    from evaluations.longmemeval.ingest.types import Session

    kwargs: dict[str, Any] = {
        "conversation_id": "conv-1",
        "source_date": date(2023, 5, 20),
        "turns": ["I switched to a standing desk."],
    }

    with pytest.raises(ValidationError):
        Session(**kwargs)


# -------------------------------------------------------------------------- Chunk


def test_chunk_is_a_pydantic_model_carrying_text_indices_and_turn_ids() -> None:
    from evaluations.longmemeval.ingest.types import Chunk

    chunk = Chunk(**_chunk_kwargs())

    assert issubclass(Chunk, BaseModel)
    assert chunk.text.startswith("[Session date: 2023-05-20]")
    assert chunk.session_idx == 0
    assert chunk.chunk_idx == 0
    assert chunk.turn_ids == ["s0:t0"]


@pytest.mark.parametrize("missing", ["text", "session_idx", "chunk_idx", "turn_ids"])
def test_chunk_requires_every_field(missing: str) -> None:
    from evaluations.longmemeval.ingest.types import Chunk

    kwargs = _chunk_kwargs()
    del kwargs[missing]

    with pytest.raises(ValidationError):
        Chunk(**kwargs)


@pytest.mark.parametrize("field", ["session_idx", "chunk_idx"])
def test_chunk_rejects_a_non_integer_index(field: str) -> None:
    """The pair is the chunk's ingest-order coordinate; a non-index makes it unorderable."""
    from evaluations.longmemeval.ingest.types import Chunk

    kwargs = _chunk_kwargs()
    kwargs[field] = "second"

    with pytest.raises(ValidationError):
        Chunk(**kwargs)


def test_chunk_rejects_a_bare_string_for_turn_ids() -> None:
    """turn_ids is the gold-overlap key (REQ-181) — a string would silently iterate chars."""
    from evaluations.longmemeval.ingest.types import Chunk

    kwargs = _chunk_kwargs()
    kwargs["turn_ids"] = "s0:t0"

    with pytest.raises(ValidationError):
        Chunk(**kwargs)


# ------------------------------------------------------------------- SourceAdapter


def test_a_second_corpus_adapter_satisfies_the_source_adapter_seam() -> None:
    """REQ-174: a new corpus costs exactly one module — this one, written inline.

    Nothing in this test imports ``evaluations.longmemeval``. If satisfying the
    seam ever required reaching into the LongMemEval package, the adapter would
    not be a seam at all.
    """
    from collections.abc import Iterator
    from datetime import date

    from evaluations.longmemeval.ingest.adapter import SourceAdapter
    from evaluations.longmemeval.ingest.types import Session, Turn

    class FakeCorpusAdapter:
        """A hypothetical second corpus, with no relationship to LongMemEval."""

        def read(self) -> Iterator[Session]:
            for index, day in enumerate((date(2023, 5, 20), date(2023, 6, 1))):
                yield Session(
                    conversation_id=f"fake-{index}",
                    source_date=day,
                    turns=[Turn(turn_id=f"fake-{index}:t0", role="user", text=f"day {index}")],
                )

    adapter: SourceAdapter = FakeCorpusAdapter()
    sessions = list(adapter.read())

    assert isinstance(FakeCorpusAdapter(), SourceAdapter)
    assert [session.conversation_id for session in sessions] == ["fake-0", "fake-1"]
    assert all(isinstance(session, Session) for session in sessions)
    assert [session.source_date for session in sessions] == [date(2023, 5, 20), date(2023, 6, 1)]


def test_a_class_without_read_does_not_satisfy_the_source_adapter_seam() -> None:
    """One method is the whole contract — so the absence of that method must fail."""
    from collections.abc import Iterator

    from evaluations.longmemeval.ingest.adapter import SourceAdapter
    from evaluations.longmemeval.ingest.types import Session

    class NotAnAdapter:
        def load(self) -> Iterator[Session]:
            yield from ()

    assert not isinstance(NotAnAdapter(), SourceAdapter)
