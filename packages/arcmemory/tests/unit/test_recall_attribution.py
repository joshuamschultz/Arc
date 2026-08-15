"""Recall must record WHICH cards it surfaced, or usefulness can never be learned.

Memory records that a recall happened and whether it returned anything. It does
not record what it returned, so nothing downstream can ask the only question that
matters for improving retrieval: did surfacing this card help?

Credit is attributed to the CARD, not the chunk. That is deliberate and follows
the 2026 result that per-item utility starves on a real store — a live agent here
holds 1,528 indexed chunks and answers a handful of turns an hour, so almost every
chunk would stay permanently cold. A card is the unit an operator edits and
consolidation merges, and there are far fewer of them.
"""

from __future__ import annotations

from arcmemory.retrieve import attributed_cards
from arcmemory.types import Recall


def _recall(source: str) -> Recall:
    return Recall(source=source, content="x", score=0.5)


def test_a_curated_card_is_attributed_by_its_slug() -> None:
    """The slug is what an operator edits and what consolidation merges."""
    recalls = [_recall("file:memory/procedures/bulk-contact-import-with-gaps.md")]

    assert attributed_cards(recalls) == ["procedures/bulk-contact-import-with-gaps"]


def test_every_kind_of_card_is_attributed() -> None:
    """Entities, insights and procedures all earn credit for being useful."""
    recalls = [
        _recall("file:memory/entities/3g-lighting.md"),
        _recall("file:memory/insights/action-vs-confirmed-receipt.md"),
    ]

    assert attributed_cards(recalls) == [
        "entities/3g-lighting",
        "insights/action-vs-confirmed-receipt",
    ]


def test_raw_stream_events_earn_no_credit() -> None:
    """A verbatim line is not a card: it cannot be improved, merged, or archived.

    Attributing to events is what would blow the dimensionality back up — they
    outnumber curated chunks 1202 to 326 on a live store and each one is seen once.
    """
    assert attributed_cards([_recall("event:07b3d1d273844b04")]) == []


def test_one_card_surfaced_twice_is_credited_once() -> None:
    """Two chunks of one card is one card's worth of usefulness, not two."""
    recalls = [
        _recall("file:memory/entities/3g-lighting.md"),
        _recall("file:memory/entities/3g-lighting.md"),
    ]

    assert attributed_cards(recalls) == ["entities/3g-lighting"]


def test_an_unrecognised_source_is_ignored_rather_than_guessed() -> None:
    """A source shape nobody planned for must not become a phantom card id."""
    assert attributed_cards([_recall("something-else")]) == []
