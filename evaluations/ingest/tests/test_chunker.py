"""RED tests for COMP-004 TurnChunker — T-785 and T-824.

Covers REQ-176 (split on turn boundaries only, ~1700 target, newline between
every turn), REQ-177 (a single over-cap turn raises rather than truncating) and
REQ-178 (every chunk carries the session date prefix).

The contract these tests pin, per SDD COMP-004::

    chunker = TurnChunker(max_event_chars=2000, target=1700)
    chunks = chunker.split(session, session_idx=0)   # -> list[Chunk]

and the chunk text layout::

    [Session date: YYYY-MM-DD]
    <turn 0 text>
    <turn 1 text>

The date prefix is its own first line and every turn is its own line. That
newline is load-bearing: ``arcmemory.security._INJECTION_RE`` deletes its match
plus everything through end of line, so one line per turn confines filter damage
to a single turn instead of the rest of the chunk.
"""

import unicodedata
from datetime import date

import pytest

from evaluations.ingest.chunker import TurnChunker, TurnExceedsCapError
from evaluations.ingest.types import Chunk, Session, Turn

# `arcmemory.security.sanitize` caps at `max_length=2000` by default; 1700 is the
# target the spec leaves beneath it so NFKC expansion plus the repeated date
# prefix still fit under the cap.
CAP = 2000
TARGET = 1700

SESSION_DATE = date(2023, 5, 20)
PREFIX = "[Session date: 2023-05-20]"

# LATIN SMALL LIGATURE FI — one character that NFKC-normalizes into two.
LIGATURE = "ﬁ"


def _turn(index: int, length: int, *, filler: str = "x") -> Turn:
    """Build a turn of exactly `length` characters whose text is unique to `index`."""
    marker = f"turn-{index:03d} "
    if length < len(marker):
        raise ValueError(f"length {length} cannot hold the {len(marker)}-char marker")
    return Turn(
        role="user" if index % 2 == 0 else "assistant",
        text=marker + filler * (length - len(marker)),
        turn_id=f"t{index:03d}",
    )


def _ligature_turn(index: int, length: int) -> Turn:
    """Build a turn of `length` characters where every tenth one expands under NFKC."""
    marker = f"turn-{index:03d} "
    pad = length - len(marker)
    body = "".join(LIGATURE if i % 10 == 0 else "x" for i in range(pad))
    return Turn(role="user", text=marker + body, turn_id=f"t{index:03d}")


def _session(turns: list[Turn]) -> Session:
    return Session(conversation_id="conv-001", source_date=SESSION_DATE, turns=turns)


def _split(session: Session, *, session_idx: int = 0) -> list[Chunk]:
    return TurnChunker(max_event_chars=CAP, target=TARGET).split(session, session_idx=session_idx)


def _body_lines(chunk: Chunk) -> list[str]:
    """The chunk's turn lines — everything after the date-prefix line."""
    return chunk.text.split("\n")[1:]


# --------------------------------------------------------------------------
# T-785 — boundary split, newline join, target size, oversized-turn raise
# --------------------------------------------------------------------------


def test_every_chunk_line_is_one_whole_turn_in_order() -> None:
    """Chunks split on turn boundaries only, one newline-separated turn per line."""
    # Arrange
    turns = [_turn(i, length=60) for i in range(6)]
    text_by_id = {turn.turn_id: turn.text for turn in turns}

    # Act
    chunks = _split(_session(turns))

    # Assert
    for chunk in chunks:
        assert _body_lines(chunk) == [text_by_id[turn_id] for turn_id in chunk.turn_ids]


def test_no_turn_text_is_ever_split_across_two_chunks() -> None:
    """A turn's full text lands intact in exactly one chunk — never mid-turn."""
    # Arrange
    turns = [_turn(i, length=421) for i in range(15)]

    # Act
    chunks = _split(_session(turns))

    # Assert
    for turn in turns:
        holders = [chunk for chunk in chunks if turn.text in chunk.text]
        assert len(holders) == 1, f"{turn.turn_id} appears in {len(holders)} chunks, not 1"


def test_turns_are_partitioned_across_chunks_in_dataset_order() -> None:
    """No turn is dropped, duplicated or reordered by the split."""
    # Arrange
    turns = [_turn(i, length=421) for i in range(15)]

    # Act
    chunks = _split(_session(turns))

    # Assert
    emitted = [turn_id for chunk in chunks for turn_id in chunk.turn_ids]
    assert emitted == [turn.turn_id for turn in turns]


def test_multi_turn_chunks_stay_within_the_target() -> None:
    """Whenever more than one turn fits, the packed chunk respects the 1700 target."""
    # Arrange
    turns = [_turn(i, length=400) for i in range(20)]

    # Act
    chunks = _split(_session(turns))

    # Assert
    for chunk in chunks:
        assert len(chunk.text) <= TARGET


def test_target_margin_absorbs_nfkc_expansion_below_the_cap() -> None:
    """The 300-char gap below `max_event_chars` survives NFKC normalization."""
    # Arrange
    turns = [_ligature_turn(i, length=400) for i in range(20)]

    # Act
    chunks = _split(_session(turns))

    # Assert
    for chunk in chunks:
        assert len(unicodedata.normalize("NFKC", chunk.text)) <= CAP


def test_single_turn_over_the_cap_raises_naming_the_turn() -> None:
    """REQ-177: an over-cap turn raises so the question can be voided."""
    # Arrange
    oversized = _turn(1, length=CAP + 1)
    session = _session([_turn(0, length=50), oversized])

    # Act / Assert
    with pytest.raises(TurnExceedsCapError) as exc_info:
        _split(session)
    assert exc_info.value.turn_id == oversized.turn_id


def test_oversized_turn_is_not_truncated_into_a_chunk() -> None:
    """The split fails outright rather than handing back a shortened turn."""
    # Arrange
    oversized = _turn(0, length=CAP + 500)
    session = _session([oversized])
    chunker = TurnChunker(max_event_chars=CAP, target=TARGET)

    # Act / Assert
    with pytest.raises(TurnExceedsCapError):
        chunker.split(session, session_idx=0)


def test_turn_above_the_target_but_under_the_cap_is_emitted_whole() -> None:
    """1700 is a packing target, not a per-turn limit — only the cap raises."""
    # Arrange
    big = _turn(0, length=1800)
    session = _session([big])

    # Act
    chunks = _split(session)

    # Assert
    assert len(chunks) == 1
    assert _body_lines(chunks[0]) == [big.text]
    assert len(chunks[0].text) <= CAP


# --------------------------------------------------------------------------
# T-824 — every chunk of a split session re-carries the session date
# --------------------------------------------------------------------------


def test_five_way_split_dates_all_five_chunks() -> None:
    """REQ-178: a session splitting into five chunks yields five dated chunks."""
    # Arrange — 15 turns of 421 chars pack three to a chunk under the 1700 target.
    turns = [_turn(i, length=421) for i in range(15)]

    # Act
    chunks = _split(_session(turns), session_idx=3)

    # Assert
    assert len(chunks) == 5
    assert [chunk.text.split("\n")[0] for chunk in chunks] == [PREFIX] * 5


def test_every_chunk_of_a_split_session_carries_its_index_and_session() -> None:
    """Each chunk is one ingest turn, so each is individually addressable."""
    # Arrange
    turns = [_turn(i, length=421) for i in range(15)]

    # Act
    chunks = _split(_session(turns), session_idx=3)

    # Assert
    assert [chunk.chunk_idx for chunk in chunks] == [0, 1, 2, 3, 4]
    assert [chunk.session_idx for chunk in chunks] == [3] * 5


def test_repeated_date_prefix_is_counted_against_the_target() -> None:
    """The prefix costs budget on every chunk, so a dated chunk never exceeds the cap."""
    # Arrange — 4 of these turns fit under 1700 only if the prefix is NOT counted.
    turns = [_turn(i, length=421) for i in range(15)]

    # Act
    chunks = _split(_session(turns), session_idx=3)

    # Assert — measured on the full dated text, prefix included.
    for chunk in chunks:
        assert chunk.text.startswith(PREFIX)
        assert len(chunk.text) <= TARGET
        assert len(chunk.text) <= CAP
