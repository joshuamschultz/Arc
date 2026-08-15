"""Curated memory must not be crowded out by the raw stream it was distilled from.

The surface index chunks the curated cards AND every raw episodic event into one
pool ranked purely on text similarity, and on a live store the raw events win: a
verbatim conversation line matches a query's wording more closely than the entity
or procedure card distilled from it, there are far more of them, and they are much
longer. Measured on a real 85-entity store, "how do we quote a customer" returned
one raw event that consumed 579 of the 1024-token budget and no procedure at all,
and "what is going on with 3G Lighting" returned two raw events rather than the
3G Lighting card.

That is the whole point of consolidation defeated at the last step: the distilled
product losing to its own source material. Raw events are still worth returning —
sometimes the verbatim line IS the answer — so they are capped rather than
dropped, which leaves room for the curated cards without discarding recall.
"""

from __future__ import annotations

from arcmemory.index.surface import _ensure_curated_present


def _curated(slug: str, score: float) -> tuple[str, float]:
    return (f"file:memory/procedures/{slug}.md", score)


def _raw(event_id: str, score: float) -> tuple[str, float]:
    return (f"event:{event_id}", score)


def test_the_curated_card_gets_a_place_when_raw_events_take_every_slot() -> None:
    """The live failure: better-matching raw lines fill the bundle and no card shows.

    The promoted card lands at the BACK of the bundle, not the front — it earns a
    place without claiming a rank it did not win.
    """
    recalls = [_raw(f"e{i}", 0.9 - i * 0.01) for i in range(5)] + [_curated("quote", 0.5)]

    kept = _ensure_curated_present(recalls, top_k=5)

    assert len(kept) == len(recalls), "a recall was dropped rather than reordered"
    head = kept[:5]
    assert any(not cid.startswith("event:") for cid, _ in head), "the curated card was crowded out"
    assert head[0][0] == "event:e0", "the best-matching line lost its lead"


def test_a_better_matching_raw_event_still_leads() -> None:
    """Capping is not demotion — relevance order is preserved among what is kept.

    Sometimes the verbatim line IS the answer, so the highest-ranked raw events
    survive the cap and keep their position.
    """
    recalls = [_raw("top", 0.99), _curated("quote", 0.5)]

    kept = _ensure_curated_present(recalls, top_k=5)

    assert kept[0][0] == "event:top"


def test_a_bundle_that_already_has_a_curated_card_is_untouched() -> None:
    """It intervenes only when curated content would be absent altogether."""
    recalls = [_curated("a", 0.9), _curated("b", 0.8), _raw("e", 0.7)]

    assert _ensure_curated_present(recalls, top_k=5) == recalls


def test_an_all_raw_pool_is_left_alone() -> None:
    """With no curated card to make room for, capping would only lose recall.

    A young agent, or a genuinely verbatim question, has nothing else to surface —
    trimming there would answer "nothing found" while the answer sat in the pool.
    """
    recalls = [_raw(f"e{i}", 0.9 - i * 0.01) for i in range(4)]

    assert _ensure_curated_present(recalls, top_k=5) == recalls
