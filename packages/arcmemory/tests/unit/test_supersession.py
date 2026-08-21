"""RED — deterministic supersession resolver (SPEC-072 COMP-007).

When two facts about the same subject conflict, recall must present the most recent as
current and MARK the older superseded — never delete the older evidence (REQ-357). The
contradiction trail (``was:``) is written at store time; ``superseded_view`` renders it
explicitly for recall, deterministically and with no LLM.
"""

from __future__ import annotations

from typing import Any

from arcmemory.index.graph import WeightedGraph
from arcmemory.stores.semantic import SemanticStore, superseded_view
from arcmemory.types import Entity, Fact

_DID = "did:arc:supersession-agent"


def _store(workspace: Any, db: Any) -> SemanticStore:
    return SemanticStore(workspace, WeightedGraph(db), scope=_DID)


def test_conflicting_fact_keeps_newest_current_and_marks_older(workspace: Any, db: Any) -> None:
    store = _store(workspace, db)
    store.write_fact("northwind", "status", "active", name="Northwind", entity_type="org")
    store.write_fact("northwind", "status", "wound down", name="Northwind", entity_type="org")

    entity = store.read("northwind")
    assert entity is not None
    fact = next(f for f in entity.facts if f.predicate == "status")
    # Newest value leads; the older is retained as a `was:` trail (mark, not delete).
    assert fact.value == "wound down"
    assert fact.was_value == "active"

    view = superseded_view(entity)
    assert any(
        "wound down" in line and "superseded" in line.lower() and "active" in line
        for line in view
    )


def test_no_conflict_produces_no_supersession(workspace: Any, db: Any) -> None:
    store = _store(workspace, db)
    store.write_fact("northwind", "status", "active", name="Northwind", entity_type="org")
    entity = store.read("northwind")
    assert entity is not None
    assert superseded_view(entity) == []


def test_superseded_view_is_pure_and_deterministic() -> None:
    """A pure function over an Entity — no store, embedder, or model on the path."""
    entity = Entity(
        slug="x",
        name="X",
        facts=[Fact(predicate="p", value="new", was_value="old", date="2026-01-02")],
    )
    assert superseded_view(entity) == superseded_view(entity)
    assert "old" in superseded_view(entity)[0]
    assert "new" in superseded_view(entity)[0]
