"""Abuse cases for entity de-dup: a merge must never launder a classification.

A fold moves every fact of one card into another. If the two sit on different
classification levels, a fold either exposes SECRET facts on an unclassified card
or buries unclassified ones under a SECRET label. These cases drive the hostile
inputs: a confirmer (the LLM) that names cards outside its cluster or across
levels, a name crafted to look like a series duplicate, the model-callable merge
primitive aimed across levels, and unknown labels at federal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.entity_dedup import EntityDeduper
from arcmemory.index.graph import WeightedGraph
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Scope


class HostileConfirmer:
    """An LLM that 'confirms' every card it has heard of as one entity."""

    def __init__(self, slugs: list[str]) -> None:
        self._slugs = slugs

    async def confirm_entity_merges(self, groups: list[Any]) -> list[list[str]]:
        return [list(self._slugs)]

    async def find_contradictions(self, group: list[Any]) -> list[str]:
        return []


def _setup(workspace: Path, db: MemoryDB, scope: Scope) -> tuple[SemanticStore, WeightedGraph]:
    graph = WeightedGraph(db)
    store = SemanticStore(workspace, graph, scope=scope.key)
    store.write_fact(
        "op-nightfall",
        "site",
        "classified location",
        name="Thesis 5: Nightfall",
        entity_type="thesis",
        classification="secret",
    )
    store.write_fact("thesis-5", "claim", "public claim", name="Thesis 5", entity_type="thesis")
    store.write_fact(
        "thesis-5-tuning",
        "claim",
        "tuning",
        name="Thesis 5: Multi-Layer Tuning",
        entity_type="thesis",
    )
    return store, graph


async def test_a_series_lookalike_never_folds_across_levels(workspace, db, scope) -> None:
    store, graph = _setup(workspace, db, scope)
    deduper = EntityDeduper(
        store, graph, scope.key, config=MemoryConfig(), confirmer=HostileConfirmer([])
    )

    await deduper.run(apply=True)

    secret = store.read("op-nightfall")
    assert secret is not None and secret.classification == "secret"
    assert [f.value for f in secret.facts] == ["classified location"]
    for slug in store.slugs():
        card = store.read(slug)
        assert card is not None
        if card.classification != "secret":
            assert all("classified" not in f.value for f in card.facts)


async def test_a_hostile_confirmer_cannot_pair_cards_across_levels(workspace, db, scope) -> None:
    store, graph = _setup(workspace, db, scope)
    store.write_fact("pantex-a", "p", "x", name="Pantex Writeup", entity_type="document")
    store.write_fact("pantex-b", "p", "y", name="Pantex Plant Writeup", entity_type="document")
    hostile = HostileConfirmer(["pantex-a", "pantex-b", "op-nightfall"])
    deduper = EntityDeduper(store, graph, scope.key, config=MemoryConfig(), confirmer=hostile)

    result = await deduper.run(apply=True)

    assert all("op-nightfall" not in pair for pair in result.merged)
    secret = store.read("op-nightfall")
    assert secret is not None and [f.value for f in secret.facts] == ["classified location"]


async def test_the_model_callable_merge_primitive_refuses_a_cross_level_fold(
    workspace, db, scope
) -> None:
    store, _ = _setup(workspace, db, scope)

    assert store.merge_into("thesis-5", "op-nightfall", strict=False) is False
    assert store.merge_into("op-nightfall", "thesis-5", strict=False) is False
    assert "op-nightfall" in store.slugs() and "thesis-5" in store.slugs()


async def test_federal_never_merges_an_unknown_label(workspace, db, scope) -> None:
    graph = WeightedGraph(db)
    store = SemanticStore(workspace, graph, scope=scope.key)
    store.write_fact(
        "t7", "c", "a", name="Thesis 7", entity_type="thesis", classification="stable"
    )
    store.write_fact("t7-x", "c", "b", name="Thesis 7: BYOA", entity_type="thesis")

    assert store.merge_into("t7-x", "t7", strict=True) is False
    deduper = EntityDeduper(store, graph, scope.key, config=MemoryConfig.for_tier("federal"))
    assert (await deduper.run(apply=True)).merged == []
    assert sorted(store.slugs()) == ["t7", "t7-x"]
