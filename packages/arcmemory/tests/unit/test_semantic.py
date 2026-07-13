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


def test_slugs_lists_entities_on_disk(workspace: Path, db: MemoryDB) -> None:
    store = _store(workspace, db)
    assert store.slugs() == []  # nothing written yet
    store.write_fact("bob", "role", "designer")
    store.write_fact("alice", "role", "engineer")
    assert store.slugs() == ["alice", "bob"]  # sorted


def test_merge_into_folds_facts_and_deletes_duplicate(workspace: Path, db: MemoryDB) -> None:
    store = _store(workspace, db)
    # Two cards for the same real-world project, minted under different slugs.
    store.write_fact("custom-erp", "vendor", "Acme", confidence=0.9)
    store.write_fact("custom-erp-system", "budget", "500k", confidence=0.8)

    assert store.merge_into("custom-erp", "custom-erp-system") is True

    survivor = store.read("custom-erp")
    assert survivor is not None
    predicates = {f.predicate: f.value for f in survivor.facts}
    assert predicates == {"vendor": "Acme", "budget": "500k"}  # both facts survive
    assert "custom-erp-system" in survivor.aliases  # fold is inspectable
    assert store.path_for("custom-erp-system").exists() is False  # duplicate gone
    assert store.slugs() == ["custom-erp"]


def test_merge_into_folds_contradiction_into_was_trail(workspace: Path, db: MemoryDB) -> None:
    store = _store(workspace, db)
    store.write_fact("acme", "hq", "Austin", confidence=0.9)
    store.write_fact("acme-inc", "hq", "Dallas", confidence=0.6)

    store.merge_into("acme", "acme-inc")

    fact = next(f for f in store.read("acme").facts if f.predicate == "hq")
    assert fact.value == "Austin"  # higher-confidence value stays current
    assert fact.was_value == "Dallas"  # loser folded, not erased


def test_merge_into_same_slug_is_noop(workspace: Path, db: MemoryDB) -> None:
    store = _store(workspace, db)
    store.write_fact("acme", "hq", "Austin")
    assert store.merge_into("acme", "acme") is False


def test_contradiction_writes_was_trail_additively(workspace: Path, db: MemoryDB) -> None:
    store = _store(workspace, db)
    store.write_fact("alice", "works_at", "Acme", confidence=0.9)
    store.write_fact("alice", "works_at", "Beta", confidence=0.6)

    body = store.path_for("alice").read_text(encoding="utf-8")
    facts = parse_facts(body)
    fact = next(f for f in facts if f.predicate == "works_at")
    assert fact.value == "Beta"  # new value wins at read time
    assert fact.was_value == "Acme"  # prior value preserved (additive, not erased)


def test_slug_variants_upsert_one_entity_not_duplicates(workspace: Path, db: MemoryDB) -> None:
    """The same entity spelled differently across runs merges into ONE record.

    The distiller proposes free-text slugs, so a project mentioned as "Custom ERP"
    one run and "custom_erp" the next must enrich the same card — never mint a
    second file (the arcui "Custom ERP x3" duplication bug).
    """
    store = _store(workspace, db)
    store.write_fact("Custom ERP", "status", "in progress")
    store.write_fact("custom_erp", "owner", "alice")
    store.write_fact("custom-erp", "status", "shipped")

    assert store.slugs() == ["custom-erp"]  # one file, not three
    entity = store.read("Custom ERP")
    assert entity is not None
    predicates = {f.predicate for f in entity.facts}
    assert predicates == {"status", "owner"}  # facts merged in place
    status = next(f for f in entity.facts if f.predicate == "status")
    assert status.value == "shipped" and status.was_value == "in progress"


def test_entity_type_enriched_in_place(workspace: Path, db: MemoryDB) -> None:
    """A better type on a later run updates the card — it does not fork identity.

    "browserbase-browse" seen as a bare thing then classified as a skill must be
    ONE entity whose type is corrected, not two rows under two types.
    """
    store = _store(workspace, db)
    store.write_fact("browserbase-browse", "seen", "yes", entity_type="thing")
    store.write_fact("browserbase-browse", "seen", "yes", entity_type="skill")

    assert store.slugs() == ["browserbase-browse"]
    entity = store.read("browserbase-browse")
    assert entity is not None and entity.entity_type == "skill"


def test_unknown_type_does_not_clobber_a_known_one(workspace: Path, db: MemoryDB) -> None:
    store = _store(workspace, db)
    store.write_fact("dana", "role", "lead", entity_type="person")
    store.write_fact("dana", "role", "lead", entity_type="unknown")

    entity = store.read("dana")
    assert entity is not None and entity.entity_type == "person"


def test_wiki_link_creates_graph_edge(workspace: Path, db: MemoryDB) -> None:
    graph = WeightedGraph(db)
    store = SemanticStore(workspace, graph, scope="did:a")
    store.write_fact("alice", "colleague", "[[bob]]", confidence=0.8)

    neighbors = dict(graph.neighbors("did:a", "alice"))
    assert "bob" in neighbors
    entity = store.read("alice")
    assert entity is not None and "[[bob]]" in entity.links_to
