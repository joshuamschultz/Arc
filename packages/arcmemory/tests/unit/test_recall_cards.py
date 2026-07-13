"""Retrieval enrichment: recall returns ranked cards with provenance + [[links]]."""

from __future__ import annotations

from pathlib import Path

from arctrust.classification import Classification

from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.retrieve import Retriever
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Scope, Situation
from tests.conftest import StubEmbedder


async def test_recall_cards_carry_provenance_and_links(
    workspace: Path, db: MemoryDB, embedder: StubEmbedder
) -> None:
    scope = Scope(agent_did="did:arc:test-agent")
    graph = WeightedGraph(db, MemoryConfig())
    semantic = SemanticStore(workspace, graph, scope=scope.key)
    # A card whose fact value points at another entity via a wiki-link.
    semantic.write_fact("brad-baker", "employer", "[[acme-corp]]", confidence=0.9)

    retriever = Retriever(db, workspace, scope, config=MemoryConfig(), embedder=embedder)
    await retriever.index()
    cards = await retriever.recall_cards(
        Situation(text="brad baker employer"),
        clearance=Classification.UNCLASSIFIED,
        top_k=5,
    )
    assert cards, "recall returned no cards"
    top = cards[0]
    # Provenance: source slug + kind + confidence are all present.
    assert top.source
    assert top.kind in top.provenance
    assert top.provenance[0] == top.source
    # Outbound link surfaced from the card content.
    assert any("acme-corp" in c.links for c in cards)
