"""Two cards describing one method must be able to become one card.

Entities get similarity clustering plus an LLM merge gate. Procedures got nothing
comparable: they merge only when their FILENAMES canonicalise to the same slug, so
two cards for the same method under different titles — "Adding a new entry to the
AI theses library" and "Logging a new AI thesis" — sit side by side forever, each
holding half the steps.

That is worse for procedures than for entities. A split entity costs a reader some
context; a split procedure means the agent follows whichever half it happens to
retrieve, and the steps in the other half simply do not happen.

Merging is non-lossy by construction: it reuses the same step merge the evolution
path uses, so a step present in only one card survives, and corroboration sums.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcmemory.stores.procedural import ProceduralStore, merge_procedures


@pytest.fixture
def store(tmp_path: Path) -> ProceduralStore:
    return ProceduralStore(tmp_path)


def _card(store: ProceduralStore, slug: str, title: str, steps: list[str], **kw: object) -> None:
    store.upsert(slug, title, steps=steps, **kw)  # type: ignore[arg-type]


def test_merging_keeps_every_step_from_both_cards(store: ProceduralStore) -> None:
    """The point: neither half's instructions may be lost."""
    _card(
        store,
        "log-a-thesis",
        "Logging a new thesis",
        ["check the index first", "verify the write"],
    )
    _card(
        store,
        "add-thesis-entry",
        "Adding a thesis entry",
        ["check the index first", "flag author bias"],
    )

    merged = merge_procedures(store, survivor="log-a-thesis", folded=["add-thesis-entry"])

    assert merged is not None
    texts = merged.step_texts
    assert "verify the write" in texts
    assert "flag author bias" in texts, "the folded card's unique step was dropped"
    assert texts.count("check the index first") == 1, "the shared step was duplicated"


def test_the_folded_card_is_removed(store: ProceduralStore) -> None:
    """Leaving it behind means the agent can still retrieve the stale half."""
    _card(store, "log-a-thesis", "Logging", ["a"])
    _card(store, "add-thesis-entry", "Adding", ["b"])

    merge_procedures(store, survivor="log-a-thesis", folded=["add-thesis-entry"])

    assert store.read("add-thesis-entry") is None
    assert {s.slug for s in store.list_summaries()} == {"log-a-thesis"}


def test_corroboration_and_use_survive_the_merge(store: ProceduralStore) -> None:
    """Both cards' evidence counts toward the surviving one.

    A step both cards carry is better evidenced than one only a single card
    mentions, and two cards' uses are two uses of the same method.
    """
    _card(store, "log-a-thesis", "Logging", ["check the index first"])
    _card(store, "add-thesis-entry", "Adding", ["check the index first"])
    store.increment_use("log-a-thesis")
    store.increment_use("add-thesis-entry")
    store.increment_use("add-thesis-entry")

    merged = merge_procedures(store, survivor="log-a-thesis", folded=["add-thesis-entry"])

    assert merged is not None
    assert merged.use_count == 3, "uses of the folded card were discarded"
    shared = next(s for s in merged.steps if s.text == "check the index first")
    assert shared.hits >= 2, "a step both cards carried gained no corroboration"


def test_the_richer_trigger_survives(store: ProceduralStore) -> None:
    """``when_to_use`` is how a procedure is FOUND — the fuller one must win."""
    _card(store, "log-a-thesis", "Logging", ["a"], when_to_use="When a thesis arrives")
    _card(
        store,
        "add-thesis-entry",
        "Adding",
        ["b"],
        when_to_use="Whenever a new AI/market thesis or a supporting article arrives",
    )

    merged = merge_procedures(store, survivor="log-a-thesis", folded=["add-thesis-entry"])

    assert merged is not None
    assert "supporting article" in merged.when_to_use


def test_merging_an_absent_card_changes_nothing(store: ProceduralStore) -> None:
    """A stale slug must not empty the survivor or raise mid-hygiene."""
    _card(store, "log-a-thesis", "Logging", ["a"])

    merged = merge_procedures(store, survivor="log-a-thesis", folded=["gone"])

    assert merged is not None
    assert merged.step_texts == ["a"]


def test_a_survivor_that_does_not_exist_is_refused(store: ProceduralStore) -> None:
    """Never mint a card from a merge — that would invent a method nobody wrote."""
    _card(store, "add-thesis-entry", "Adding", ["b"])

    assert merge_procedures(store, survivor="nope", folded=["add-thesis-entry"]) is None
    assert store.read("add-thesis-entry") is not None, "the folded card was destroyed anyway"
