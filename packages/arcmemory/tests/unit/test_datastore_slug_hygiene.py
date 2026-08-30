"""H-024b — stale ``db_table`` slug hygiene.

H-024 moved the persisted datastore ontology from a global ``db-table-<name>``
slug to a source-scoped ``db-table-<source_id>-<name>``. Deployed boxes carry
months of OLD-shape rows that nothing overwrote, and ``unregister_datastore``
only ever removed ``source-<id>`` — leaking ``db_table`` cards on revoke
(pre-existing). Both pollute ``list_datastore_tables`` (duplicate/stale cards in
the Datastore tab) and recall (the entities subdir is indexed).

The fix purges by FACT equality (never slug prefix, which would let
``db-table-a-…`` over-purge a source named ``a-b``): a card with no ``source_id``
fact is a dead pre-scoping shape; a card owned by this source whose table left
the live ontology is a dropped table. ``unregister`` runs the same purge with an
empty ontology, closing the revoke leak. Convergence is automatic: the
coordinator re-registers each live datastore per sync.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from arcmemory.brain import ArcMemoryBrain
from arcmemory.operator import MemoryOperator
from arcmemory.stores.semantic import SemanticStore

_DID = "did:arc:hygiene-agent"
_SOURCE_A = "acct-a"
_SOURCE_B = "acct-b"


def _brain(workspace: Path) -> ArcMemoryBrain:
    return ArcMemoryBrain(workspace, _DID)


def _store(brain: ArcMemoryBrain) -> SemanticStore:
    # Same scope/graph/workspace register_datastore writes db_table cards to.
    return SemanticStore(brain._workspace, brain._graph, brain._scope(None).key)


async def _register(brain: ArcMemoryBrain, source_id: str, *tables: str) -> None:
    conn = sqlite3.connect(":memory:")
    for table in tables:
        conn.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY, amount TEXT)")
    conn.commit()
    await brain.register_sqlite_datastore(source_id, conn)


def _db_table_slugs(brain: ArcMemoryBrain) -> set[str]:
    store = _store(brain)
    return {
        slug
        for slug in store.slugs()
        if (e := store.read(slug)) is not None and e.entity_type == "db_table"
    }


def _seed_pre_scoping_card(brain: ArcMemoryBrain, name: str) -> str:
    """Write an OLD-shape card (``db-table-<name>``, no source_id fact)."""
    slug = f"db-table-{name}"
    _store(brain).write_fact(slug, "primary_key", "id", entity_type="db_table")
    return slug


def _seed_scoped_card(brain: ArcMemoryBrain, source_id: str, table: str) -> str:
    """Write a scoped card for another source, exactly as persist_ontology would."""
    slug = f"db-table-{source_id}-{table}"
    store = _store(brain)
    store.write_fact(slug, "source_id", source_id, entity_type="db_table")
    store.write_fact(slug, "table_name", table, entity_type="db_table")
    return slug


async def test_pre_scoping_card_purged_on_register(workspace: Path) -> None:
    brain = _brain(workspace)
    legacy = _seed_pre_scoping_card(brain, "invoices")
    assert legacy in _db_table_slugs(brain)  # seeded

    await _register(brain, _SOURCE_A, "invoices")

    slugs = _db_table_slugs(brain)
    assert legacy not in slugs  # dead pre-scoping shape collected
    assert f"db-table-{_SOURCE_A}-invoices" in slugs  # new scoped card present


async def test_dropped_table_card_purged_on_reregister(workspace: Path) -> None:
    brain = _brain(workspace)
    await _register(brain, _SOURCE_A, "invoices", "customers")
    assert f"db-table-{_SOURCE_A}-customers" in _db_table_slugs(brain)

    await _register(brain, _SOURCE_A, "invoices")  # customers dropped

    slugs = _db_table_slugs(brain)
    assert f"db-table-{_SOURCE_A}-customers" not in slugs
    assert f"db-table-{_SOURCE_A}-invoices" in slugs


async def test_other_source_card_not_purged_on_register(workspace: Path) -> None:
    # A source named to collide on a prefix: purging source "acct-a" must not
    # touch "acct-a-b"'s cards. Fact equality, never prefix.
    brain = _brain(workspace)
    other = _seed_scoped_card(brain, "acct-a-b", "orders")

    await _register(brain, _SOURCE_A, "invoices")

    assert other in _db_table_slugs(brain)  # different source, untouched


async def test_unregister_purges_this_sources_cards(workspace: Path) -> None:
    brain = _brain(workspace)
    await _register(brain, _SOURCE_A, "invoices", "customers")
    assert _db_table_slugs(brain)  # cards exist

    assert await brain.unregister_datastore(_SOURCE_A) is True

    remaining = {s for s in _db_table_slugs(brain) if s.startswith(f"db-table-{_SOURCE_A}-")}
    assert remaining == set()  # revoke leak closed


async def test_unregister_leaves_other_source_cards(workspace: Path) -> None:
    brain = _brain(workspace)
    await _register(brain, _SOURCE_A, "invoices")
    other = _seed_scoped_card(brain, _SOURCE_B, "orders")

    await brain.unregister_datastore(_SOURCE_A)

    assert other in _db_table_slugs(brain)  # source B untouched


async def test_purged_cards_gone_from_operator_table_list(workspace: Path) -> None:
    brain = _brain(workspace)
    _seed_pre_scoping_card(brain, "invoices")
    await _register(brain, _SOURCE_A, "invoices")

    # The Datastore tab (unscoped list) must not show a stale duplicate.
    names = [t.name for t in MemoryOperator(workspace, _DID).list_datastore_tables()]
    assert names.count("invoices") == 1


async def test_register_overlays_and_explorer_browses_same_source(workspace: Path) -> None:
    # Reconciliation of H-024 (scoped slug + explorer) with H-025 (semantic-layer
    # overlay) on ONE register_datastore call: the overlay file is written AND the
    # explorer lists exactly that source's tables from the persisted, scoped cards.
    from arcmemory.semantic_layer import layer_path

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE invoices (id TEXT PRIMARY KEY, amount TEXT)")
    conn.commit()
    brain = _brain(workspace)
    await brain.register_sqlite_datastore(
        _SOURCE_A, conn, connection_id="shop-db", caller_did=_DID
    )

    # H-025: the editable semantic-layer file exists for this connection.
    path = layer_path("shop-db")
    assert path is not None and path.exists()

    # H-024: the explorer sees exactly this source's tables, scoped-slug shaped.
    tables = MemoryOperator(workspace, _DID).list_datastore_tables(source_id=_SOURCE_A)
    assert [t.name for t in tables] == ["invoices"]
    assert all(t.slug.startswith(f"db-table-{_SOURCE_A}-") for t in tables)
