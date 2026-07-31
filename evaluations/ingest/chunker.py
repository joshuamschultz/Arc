"""TurnChunker (COMP-004 / REQ-176, REQ-177, REQ-178).

Packs whole turns into dated chunks, one chunk per ingest turn. Three properties
carry the whole design:

*One line per turn.* ``arcmemory.security._INJECTION_RE`` deletes its match plus
everything through end of line, and ordinary benchmark prose trips it. Giving
each turn its own line confines that deletion to the turn that triggered it
instead of the remainder of the chunk.

*Every chunk re-carries the date.* One chunk is fed as one agent turn, so a
retrieval landing on the fourth chunk of a session must still see the session's
date. The prefix is the chunk's own first line and costs budget on every chunk,
not just the first.

*Raise, never truncate.* ``sanitize`` caps at the configured
``max_event_chars`` (``evaluations.ingest.limits``) and the capture path
discards its return value, so a turn that overflows the cap would be silently
shortened and scored as a memory failure. A turn that cannot be dated and stay
under the cap voids its question instead (REQ-177).

The 1700 default target is a packing budget, not a second cap: it keeps a
multi-turn chunk near the size the recall budget is sized against. A single turn
larger than it still ships whole — only ``max_event_chars`` raises.
"""

from __future__ import annotations

from evaluations.ingest.types import Chunk, Session, Turn


class TurnExceedsCapError(Exception):
    """One turn cannot be dated and stay under ``max_event_chars`` (REQ-177).

    Carries ``turn_id`` so the caller can void the question with reason
    ``turn_exceeds_cap`` and name the evidence that would have been destroyed.
    """

    def __init__(self, turn_id: str) -> None:
        super().__init__(
            f"turn {turn_id!r} does not fit under max_event_chars once dated; "
            "the question must be voided rather than the turn truncated"
        )
        self.turn_id = turn_id


class TurnChunker:
    """Split a session into dated chunks on turn boundaries only."""

    def __init__(self, *, max_event_chars: int, target: int = 1700) -> None:
        if target > max_event_chars:
            raise ValueError(
                f"target {target} exceeds max_event_chars {max_event_chars}; "
                "packing to it would produce chunks sanitize() truncates"
            )
        self._max_event_chars = max_event_chars
        self._target = target

    def split(self, session: Session, *, session_idx: int) -> list[Chunk]:
        """Return the session's chunks in dataset order, every one dated.

        Turns are packed greedily up to ``target`` and never divided: a turn
        that alone overflows the target still ships whole, because the target
        is a packing budget and only ``max_event_chars`` is a hard limit.
        """
        prefix = f"[Session date: {session.source_date.isoformat()}]"
        chunks: list[Chunk] = []
        batch: list[Turn] = []
        used = len(prefix)

        for turn in session.turns:
            cost = 1 + len(turn.text)  # the turn's own line, its newline included
            if len(prefix) + cost > self._max_event_chars:
                raise TurnExceedsCapError(turn.turn_id)
            if batch and used + cost > self._target:
                chunks.append(_chunk(prefix, batch, session_idx, len(chunks)))
                batch = []
                used = len(prefix)
            batch.append(turn)
            used += cost

        if batch:
            chunks.append(_chunk(prefix, batch, session_idx, len(chunks)))
        return chunks


def _chunk(prefix: str, turns: list[Turn], session_idx: int, chunk_idx: int) -> Chunk:
    """Render one packed batch as a dated chunk, one turn per line."""
    return Chunk(
        text="\n".join([prefix, *(turn.text for turn in turns)]),
        session_idx=session_idx,
        chunk_idx=chunk_idx,
        turn_ids=[turn.turn_id for turn in turns],
    )
