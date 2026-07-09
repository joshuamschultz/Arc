"""T-022 — semantic store: triplets, additive `was:` trail, wiki-link edge."""

from __future__ import annotations

from pathlib import Path

from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.stores.semantic import SemanticStore, parse_facts


def _store(workspace: Path, db: MemoryDB) -> SemanticStore:
    return SemanticStore(workspace, WeightedGraph(db), scope="did:a")


def test_fact_written_and_parsed(workspace: Path, db: MemoryDB) -> None:
    store = _store(workspace, db)
    store.write_fact("alice", "works_at", "Acme", confidence=0.9)
    entity = store.read("alice")
    assert entity is not None
    assert entity.facts[0].predicate == "works_at"
    assert entity.facts[0].value == "Acme"


def test_contradiction_writes_was_trail_additively(workspace: Path, db: MemoryDB) -> None:
    store = _store(workspace, db)
    store.write_fact("alice", "works_at", "Acme", confidence=0.9)
    store.write_fact("alice", "works_at", "Beta", confidence=0.6)

    body = store.path_for("alice").read_text(encoding="utf-8")
    facts = parse_facts(body)
    fact = next(f for f in facts if f.predicate == "works_at")
    assert fact.value == "Beta"  # new value wins at read time
    assert fact.was_value == "Acme"  # prior value preserved (additive, not erased)


def test_wiki_link_creates_graph_edge(workspace: Path, db: MemoryDB) -> None:
    graph = WeightedGraph(db)
    store = SemanticStore(workspace, graph, scope="did:a")
    store.write_fact("alice", "colleague", "[[bob]]", confidence=0.8)

    neighbors = dict(graph.neighbors("did:a", "alice"))
    assert "bob" in neighbors
    entity = store.read("alice")
    assert entity is not None and "[[bob]]" in entity.links_to
