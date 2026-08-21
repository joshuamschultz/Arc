"""RED — the temporal_enabled off-switch restores pre-temporal recall (SPEC-072 T-1017).

``temporal_enabled=False`` must return recall to its exact pre-SPEC-072 behavior: cards
carry NO establishment stamp, and recency is NOT used as a tie-break (ties fall back to
the source-id order). Anything less makes the toggle a dead field (the producers-unwired
trap). REQ-362.
"""

from __future__ import annotations

from typing import Any

from arctrust.classification import Classification
from packages.arcmemory.tests.conftest import StubEmbedder

from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.retrieve import Retriever, _rrf_fuse
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Recall, Scope, Situation

_DID = "did:arc:temporal-off-agent"


async def test_temporal_disabled_leaves_cards_unstamped(
    workspace: Any, db: MemoryDB, embedder: StubEmbedder
) -> None:
    scope = Scope(agent_did=_DID)
    store = SemanticStore(workspace, WeightedGraph(db), scope=scope.key)
    store.write_fact("aurora", "kind", "candidate", name="Aurora", entity_type="thing")

    retr = Retriever(
        db, workspace, scope, config=MemoryConfig(temporal_enabled=False), embedder=embedder
    )
    await retr.index()
    cards = await retr.recall_cards(
        Situation(text="aurora"), clearance=Classification.UNCLASSIFIED, top_k=5
    )

    assert cards, "recall returned nothing"
    assert all(c.established == "" for c in cards), "temporal off must leave cards unstamped"


def test_recency_tiebreak_off_falls_back_to_source_order() -> None:
    older = Recall(source="aaa-old", content="x", score=0.0, established="2020-01-01")
    newer = Recall(source="zzz-new", content="y", score=0.0, established="2026-01-01")

    # recency=False → the pre-temporal alphabetical tie-break, NOT recency.
    fused = _rrf_fuse([[older], [newer]], recency=False)
    assert [r.source for r in fused] == ["aaa-old", "zzz-new"]

    # recency=True (default) still puts the newer first (regression guard).
    fused_on = _rrf_fuse([[older], [newer]], recency=True)
    assert [r.source for r in fused_on] == ["zzz-new", "aaa-old"]
