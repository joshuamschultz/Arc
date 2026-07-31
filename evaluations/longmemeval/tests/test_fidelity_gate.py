"""RED tests for COMP-005 SanitizeFidelityGate — T-794.

Covers REQ-180 (run every chunk through ``arcmemory.security.sanitize`` and
``privacy_filter`` locally before ingest and compare what came back) and REQ-181
(damage overlapping a gold-evidence turn voids the question).

These tests import the REAL ``sanitize`` and ``privacy_filter`` and never mock
them. The gate exists to detect what the *live* filters destroy, so a stub would
pin the spec's guess about arcmemory rather than arcmemory's behavior, and would
keep passing on the day arcmemory's patterns change. Every chunk here is built
by the real ``TurnChunker`` too, so the one-line-per-turn layout the damage is
attributed against is the layout ingest actually produces.

The contract these tests pin, per SDD COMP-005::

    gate = SanitizeFidelityGate()
    verdict = gate.check(chunk, gold_turn_ids={"t2"})
    # -> FidelityVerdict(ok, shrunk_by, gold_overlap, spans)

``spans`` are half-open ``(start, stop)`` offsets into ``chunk.text`` — the
ORIGINAL text — which is what lets a destroyed region be attributed back to the
turn whose line it landed on.

``shrunk_by`` counts original characters destroyed, not the length delta.
``privacy_filter`` substitutes a ten-character ``[REDACTED]``, so redacting the
nine characters ``"secret: I"`` leaves the text one character LONGER while
destroying nine characters of evidence. A literal reading of REQ-180's "compare
the output length to the input length" reports no shrink there and waves a
destroyed gold turn through, so the gate has to diff rather than measure.
"""

from datetime import date

import pytest
from arcmemory.security import privacy_filter, sanitize

from evaluations.longmemeval.ingest.chunker import TurnChunker
from evaluations.longmemeval.ingest.fidelity import FidelityVerdict, SanitizeFidelityGate
from evaluations.longmemeval.ingest.types import Chunk, Session, Turn

CAP = 2000
SESSION_DATE = date(2023, 5, 20)

# Ordinary benchmark prose that the live filters destroy. Both are answer-bearing
# assistant/user turns of exactly the kind LongMemEval marks as gold evidence.
CLEAN_TURN = "I finally finished the PMP exam prep course."
INJECTION_TURN = "Congratulations! You are now a certified PM as of May 2023."
INJECTION_TAIL = "You are now a certified PM as of May 2023."
SECRET_TURN = "My secret: I actually hate cilantro"


def _chunk(*turns: tuple[str, str]) -> Chunk:
    """Build a single chunk through the real chunker from ``(turn_id, text)`` pairs."""
    session = Session(
        conversation_id="c1",
        source_date=SESSION_DATE,
        turns=[Turn(turn_id=turn_id, role="user", text=text) for turn_id, text in turns],
    )
    chunks = TurnChunker(max_event_chars=CAP).split(session, session_idx=0)
    assert len(chunks) == 1, "fixture must fit one chunk or the offsets under test move"
    return chunks[0]


def _destroyed(chunk: Chunk, verdict: FidelityVerdict) -> str:
    """Concatenate the original text the verdict reports as destroyed."""
    return "".join(chunk.text[start:stop] for start, stop in verdict.spans)


@pytest.fixture
def gate() -> SanitizeFidelityGate:
    return SanitizeFidelityGate()


# ---------------------------------------------------------------------------
# The live filters — asserted directly, so the premise is never assumed.
# ---------------------------------------------------------------------------


def test_live_sanitize_deletes_the_injection_match_through_end_of_line() -> None:
    """``_INJECTION_RE`` eats its match plus the rest of the line (REQ-180)."""
    assert sanitize(INJECTION_TURN) == "Congratulations!"


def test_live_privacy_filter_redacts_ordinary_prose_mid_sentence() -> None:
    """``_SECRET_PATTERNS`` redacts plain prose, not just credentials (REQ-180)."""
    assert privacy_filter(SECRET_TURN) == "My [REDACTED] actually hate cilantro"


def test_live_privacy_filter_redaction_makes_the_text_longer() -> None:
    """The length delta is NEGATIVE here, so length alone cannot detect damage."""
    assert len(privacy_filter(SECRET_TURN)) > len(SECRET_TURN)


# ---------------------------------------------------------------------------
# The gate.
# ---------------------------------------------------------------------------


def test_undamaged_chunk_passes_clean(gate: SanitizeFidelityGate) -> None:
    chunk = _chunk(("t1", CLEAN_TURN), ("t2", "I passed it on the first try."))

    verdict = gate.check(chunk, gold_turn_ids={"t1", "t2"})

    assert verdict.ok is True
    assert verdict.shrunk_by == 0
    assert verdict.gold_overlap is False
    assert verdict.spans == []


def test_injection_damage_on_a_gold_turn_sets_gold_overlap(gate: SanitizeFidelityGate) -> None:
    """Gold evidence deleted by ``_INJECTION_RE`` voids the question (REQ-181)."""
    chunk = _chunk(("t1", CLEAN_TURN), ("t2", INJECTION_TURN))

    verdict = gate.check(chunk, gold_turn_ids={"t2"})

    assert verdict.ok is False
    assert verdict.gold_overlap is True
    assert verdict.shrunk_by >= len(INJECTION_TAIL)
    assert INJECTION_TAIL in _destroyed(chunk, verdict)


def test_redaction_on_a_gold_turn_sets_gold_overlap_though_the_text_grows(
    gate: SanitizeFidelityGate,
) -> None:
    """Damage is characters destroyed, not length lost — the trap in REQ-180."""
    chunk = _chunk(("t1", CLEAN_TURN), ("t2", SECRET_TURN))
    filtered = privacy_filter(sanitize(chunk.text))
    assert len(filtered) > len(chunk.text), "fixture must be the length-grows case"

    verdict = gate.check(chunk, gold_turn_ids={"t2"})

    assert verdict.ok is False
    assert verdict.gold_overlap is True
    assert verdict.shrunk_by > 0
    assert "secret" in _destroyed(chunk, verdict)


def test_damage_on_a_non_gold_turn_warns_instead_of_voiding(gate: SanitizeFidelityGate) -> None:
    """Same damage, different turn: a warning on the row, not a void (REQ-181)."""
    chunk = _chunk(("t1", CLEAN_TURN), ("t2", INJECTION_TURN))

    verdict = gate.check(chunk, gold_turn_ids={"t1"})

    assert verdict.gold_overlap is False
    assert verdict.shrunk_by > 0
    assert verdict.ok is False


def test_verdict_carries_the_four_documented_fields(gate: SanitizeFidelityGate) -> None:
    chunk = _chunk(("t1", CLEAN_TURN), ("t2", INJECTION_TURN))

    verdict = gate.check(chunk, gold_turn_ids={"t2"})

    assert isinstance(verdict, FidelityVerdict)
    assert isinstance(verdict.ok, bool)
    assert isinstance(verdict.shrunk_by, int)
    assert isinstance(verdict.gold_overlap, bool)
    assert isinstance(verdict.spans, list)
    for span in verdict.spans:
        start, stop = span
        assert isinstance(start, int)
        assert isinstance(stop, int)
        assert 0 <= start < stop <= len(chunk.text)
