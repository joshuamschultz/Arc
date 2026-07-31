"""SanitizeFidelityGate (COMP-005 / REQ-180, REQ-181).

The single highest-value safeguard in the harness. Every chunk goes through the
LIVE ``arcmemory.security.sanitize`` and ``privacy_filter`` — the same pair
``arcmemory.capture`` applies, imported rather than reimplemented, so the gate
reports what the real ingest path will do rather than what this spec guessed it
would do.

*Damage is diffed, not measured.* REQ-180 says to compare the output length to
the input length, but ``privacy_filter`` substitutes a ten-character
``[REDACTED]``, so redacting the nine characters ``"secret: I"`` leaves the text
one character LONGER while destroying nine characters of evidence. A length
comparison waves that through and the question then scores as a memory failure.
The gate diffs, and counts original characters destroyed.

*Cosmetic rewrites are not damage.* ``sanitize`` also collapses runs of spaces,
strips the edges, and NFKC-normalizes. None of that destroys evidence, and
counting it would void questions over a double space after a period — burying
the injection signal this gate exists to surface. A rewritten span is cleared
when it is whitespace only, or when normalizing it yields exactly what replaced
it.

*Damage is attributed by line.* COMP-004 gives every turn its own line, so the
line a destroyed span lands on names the turn it damaged. A turn whose own text
carries a newline breaks that layout; the gate then treats the whole chunk as
gold whenever the chunk carries any gold turn, because voiding a question that
might have scored is the safe error and scoring destroyed evidence is not.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from arcmemory.security import privacy_filter, sanitize

from evaluations.ingest.types import Chunk


class GoldEvidenceFilteredError(Exception):
    """The live filters destroyed a turn the question's gold evidence names.

    Raised by the ingest driver on ``gold_overlap`` (REQ-181). ``reason`` is the
    verbatim string the voided result row records, so the void is legible as a
    harness limitation rather than as a memory failure.
    """

    reason = "gold_evidence_filtered"


@dataclass(frozen=True)
class FidelityVerdict:
    """What the live filters did to one chunk.

    ``spans`` are half-open ``(start, stop)`` offsets into the ORIGINAL chunk
    text, which is what lets a destroyed region be attributed back to a turn.
    ``shrunk_by`` is the count of original characters destroyed, never the
    length delta.
    """

    ok: bool
    shrunk_by: int
    gold_overlap: bool
    spans: list[tuple[int, int]]


class SanitizeFidelityGate:
    """Check a chunk against the live filters before it is ingested."""

    def __init__(self, *, max_event_chars: int = 2000) -> None:
        # Mirrors ``arcmemory.capture``, which calls sanitize with the configured
        # cap; a gate on the default cap would miss truncation the real path does.
        self._max_event_chars = max_event_chars

    def check(self, chunk: Chunk, *, gold_turn_ids: set[str]) -> FidelityVerdict:
        """Report what ``sanitize`` then ``privacy_filter`` destroy in ``chunk``."""
        filtered = privacy_filter(sanitize(chunk.text, max_length=self._max_event_chars))
        spans = _destroyed_spans(chunk.text, filtered)
        gold_ranges = _gold_ranges(chunk, gold_turn_ids)
        return FidelityVerdict(
            ok=not spans,
            shrunk_by=sum(stop - start for start, stop in spans),
            gold_overlap=any(_overlaps(span, gold) for span in spans for gold in gold_ranges),
            spans=spans,
        )


def _destroyed_spans(original: str, filtered: str) -> list[tuple[int, int]]:
    """Offsets into ``original`` whose characters did not survive the filters.

    ``autojunk`` is off: it would treat every character appearing in more than
    one percent of a 1700-character chunk — the space, most vowels — as junk and
    mis-align the diff exactly where the damage is.
    """
    matcher = SequenceMatcher(a=original, b=filtered, autojunk=False)
    return [
        (i1, i2)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag in {"delete", "replace"} and not _survives(original[i1:i2], filtered[j1:j2])
    ]


def _survives(original: str, replacement: str) -> bool:
    """True when a rewritten span carries no evidence loss (spacing, NFKC)."""
    return original.isspace() or unicodedata.normalize("NFKC", original) == replacement


def _gold_ranges(chunk: Chunk, gold_turn_ids: set[str]) -> list[tuple[int, int]]:
    """Offset ranges of ``chunk.text`` holding turns the gold evidence names."""
    lines = chunk.text.split("\n")
    if len(lines) - 1 != len(chunk.turn_ids):
        # A turn's own text carried a newline, so no line names a single turn.
        return [(0, len(chunk.text))] if gold_turn_ids & set(chunk.turn_ids) else []

    ranges: list[tuple[int, int]] = []
    offset = len(lines[0]) + 1  # line 0 is the chunker's date prefix, never a turn
    for line, turn_id in zip(lines[1:], chunk.turn_ids, strict=True):
        if turn_id in gold_turn_ids:
            ranges.append((offset, offset + len(line)))
        offset += len(line) + 1
    return ranges


def _overlaps(span: tuple[int, int], other: tuple[int, int]) -> bool:
    """True when two half-open ranges share at least one character."""
    return span[0] < other[1] and other[0] < span[1]
