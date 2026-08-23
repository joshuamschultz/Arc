"""RED — SPEC-073 Phase A (A1): MemoryOperator connector-data view accessors.

``MemoryOperator`` today exposes only the memory/entity/graph surface
(``list_entries``, ``list_entities``, ``search``, ...). SPEC-073 A1 adds a
second, connector-data-shaped surface an arcui "Knowledge" offshoot will
read: registered sources, their committed mappings, blob-storage folder
ontology, live datastore table ontology, per-source document search,
canonical-item provenance, semantic-channel health, and a live reopened
datastore query.

None of ``list_sources`` / ``get_source_mapping`` / ``list_mappings`` /
``list_blob_folders`` / ``list_datastore_tables`` / ``document_search`` /
``list_provenances`` / ``index_health`` / ``datastore_query`` exist yet on
``MemoryOperator`` — every test below is expected to fail with
``AttributeError`` (the accessor is simply missing), not a setup error. The
workspace seeding uses ONLY the real ingest-side primitives (``ingest.
register_source``, ``mapping.commit_mapping``, ``blob_ontology.
walk_blob_source``, ``DocIndex.index_source``, ``ProvenanceStore.record``,
``ArcMemoryBrain.register_datastore``) — no hand-rolled SQL, no mocks of
arcmemory's own stores.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from arcmemory import ingest
from arcmemory.blob_ontology import BlobObject, walk_blob_source
from arcmemory.brain import ArcMemoryBrain
from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.doc_index import DocIndex
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.source import SourceChunk
from arcmemory.mapping import commit_mapping
from arcmemory.operator import MemoryOperator
from arcmemory.stores.provenance import ProvenanceStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Provenance, Scope, SourceMapping

_DID = "did:arc:connector-test"
_SOURCE_ID = "acct-1"
_MEM_SOURCE_ID = "mem-only-source"
_ITEM_ID = "canonical-item-1"


def _scope() -> Scope:
    return Scope(agent_did=_DID)


async def _seed_workspace(workspace: Path, datastore_path: Path) -> None:
    """Seed a real workspace end to end via the ingest-side primitives.

    Registers ``_SOURCE_ID``, commits its mapping, walks a blob folder,
    indexes one document chunk, records two provenances for one canonical
    item, and registers a live sqlite FILE datastore (persisted reopen path)
    plus a second, in-memory-only datastore (no reopen path).
    """
    graph = WeightedGraph(MemoryDB(workspace))
    scope = _scope()

    ingest.register_source(workspace, graph, scope, _SOURCE_ID, kind="quickbooks")

    store = SemanticStore(workspace, graph, scope.key)
    commit_mapping(
        SourceMapping(source_id=_SOURCE_ID, homes=["document", "datastore"]), store=store
    )

    walk_blob_source(
        [BlobObject(path="invoices/2026-01.pdf", mime="application/pdf", size=1024)],
        source_id=_SOURCE_ID,
        store=store,
    )

    cfg = MemoryConfig()
    db = MemoryDB(workspace)
    await DocIndex(db, workspace, cfg).index_source(
        _SOURCE_ID,
        _DID,
        [
            SourceChunk(
                chunk_id="chunk-inv-001",
                source_path=f"{_SOURCE_ID}:inv-001",
                text="Invoice 001 for Acme Corp, amount 500",
                classification="unclassified",
                mtime=0.0,
            )
        ],
    )

    provenance_store = ProvenanceStore(db)
    provenance_store.record(
        _ITEM_ID,
        Provenance(source=_SOURCE_ID, external_id="inv-001", classification="unclassified"),
    )
    provenance_store.record(
        _ITEM_ID,
        Provenance(source="other-source", external_id="ext-9", classification="unclassified"),
    )

    brain = ArcMemoryBrain(workspace, _DID)

    conn = sqlite3.connect(str(datastore_path))
    conn.execute("CREATE TABLE invoices (id TEXT PRIMARY KEY, amount TEXT)")
    conn.execute("INSERT INTO invoices (id, amount) VALUES ('001', '500')")
    conn.commit()
    await brain.register_sqlite_datastore(_SOURCE_ID, conn)

    mem_conn = sqlite3.connect(":memory:")
    mem_conn.execute("CREATE TABLE widgets (id TEXT PRIMARY KEY)")
    mem_conn.commit()
    await brain.register_sqlite_datastore(_MEM_SOURCE_ID, mem_conn)


@pytest.fixture
async def seeded_workspace(workspace: Path, tmp_path: Path) -> Path:
    """A workspace seeded through the real ingest side (fixture ``workspace`` from conftest)."""
    datastore_path = tmp_path / "connected-accounting.sqlite"
    await _seed_workspace(workspace, datastore_path)
    return workspace


# ---------------------------------------------------------------------------
# list_sources / get_source_mapping / list_mappings
# ---------------------------------------------------------------------------


def test_list_sources_returns_the_seeded_registration(seeded_workspace: Path) -> None:
    operator = MemoryOperator(seeded_workspace, _DID)

    sources = operator.list_sources()

    slugs = {s.slug for s in sources}
    assert f"source-{_SOURCE_ID}" in slugs


def test_get_source_mapping_returns_the_committed_homes(seeded_workspace: Path) -> None:
    operator = MemoryOperator(seeded_workspace, _DID)

    mapping = operator.get_source_mapping(_SOURCE_ID)

    assert mapping is not None
    assert mapping.source_id == _SOURCE_ID
    assert set(mapping.homes) == {"document", "datastore"}


def test_get_source_mapping_missing_source_is_none(seeded_workspace: Path) -> None:
    operator = MemoryOperator(seeded_workspace, _DID)

    assert operator.get_source_mapping("never-mapped") is None


def test_list_mappings_returns_every_committed_mapping(seeded_workspace: Path) -> None:
    operator = MemoryOperator(seeded_workspace, _DID)

    mappings = operator.list_mappings()

    assert any(m.source_id == _SOURCE_ID for m in mappings)


# ---------------------------------------------------------------------------
# list_blob_folders
# ---------------------------------------------------------------------------


def test_list_blob_folders_returns_the_walked_folder(seeded_workspace: Path) -> None:
    operator = MemoryOperator(seeded_workspace, _DID)

    folders = operator.list_blob_folders()

    assert any(f.entity_type == "blob_folder" for f in folders)


def test_list_blob_folders_scoped_to_source_id(seeded_workspace: Path) -> None:
    operator = MemoryOperator(seeded_workspace, _DID)

    folders = operator.list_blob_folders(_SOURCE_ID)

    assert folders and all(f.slug.startswith(f"blob-{_SOURCE_ID}-") for f in folders)


def test_list_blob_folders_scoped_to_unknown_source_is_empty(seeded_workspace: Path) -> None:
    operator = MemoryOperator(seeded_workspace, _DID)

    assert operator.list_blob_folders("no-such-source") == []


# ---------------------------------------------------------------------------
# list_datastore_tables
# ---------------------------------------------------------------------------


def test_list_datastore_tables_returns_the_introspected_table(seeded_workspace: Path) -> None:
    operator = MemoryOperator(seeded_workspace, _DID)

    tables = operator.list_datastore_tables()

    slugs = {t.slug for t in tables}
    assert "db-table-invoices" in slugs


# ---------------------------------------------------------------------------
# document_search
# ---------------------------------------------------------------------------


async def test_document_search_finds_the_indexed_chunk(seeded_workspace: Path) -> None:
    operator = MemoryOperator(seeded_workspace, _DID)

    hits = await operator.document_search(_SOURCE_ID, "Acme")

    assert any("Acme" in hit.text for hit in hits)
    assert all(hit.source_id == _SOURCE_ID for hit in hits)


async def test_document_search_scoped_to_a_different_source_finds_nothing(
    seeded_workspace: Path,
) -> None:
    operator = MemoryOperator(seeded_workspace, _DID)

    hits = await operator.document_search("unrelated-source", "Acme")

    assert hits == []


# ---------------------------------------------------------------------------
# list_provenances
# ---------------------------------------------------------------------------


def test_list_provenances_returns_both_recorded_provenances(seeded_workspace: Path) -> None:
    operator = MemoryOperator(seeded_workspace, _DID)

    provenances = operator.list_provenances(_ITEM_ID)

    assert len(provenances) == 2
    sources = {p.source for p in provenances}
    assert sources == {_SOURCE_ID, "other-source"}


def test_list_provenances_unknown_item_is_empty(seeded_workspace: Path) -> None:
    operator = MemoryOperator(seeded_workspace, _DID)

    assert operator.list_provenances("never-recorded") == []


# ---------------------------------------------------------------------------
# index_health
# ---------------------------------------------------------------------------


async def test_index_health_returns_a_semantic_status(seeded_workspace: Path) -> None:
    from arcmemory.status import SemanticStatus

    operator = MemoryOperator(seeded_workspace, _DID)

    status = await operator.index_health()

    assert isinstance(status, SemanticStatus)
    # No embedder was injected -- arcui builds the operator embedder-less, so
    # the probe must honestly report the degraded channel, never fake "live".
    assert status.embedder_live is False


# ---------------------------------------------------------------------------
# datastore_query (the persisted reopen path + the in-memory degrade case)
# ---------------------------------------------------------------------------


def test_datastore_query_reopens_the_persisted_file_and_returns_the_row(
    seeded_workspace: Path,
) -> None:
    operator = MemoryOperator(seeded_workspace, _DID)

    row = operator.datastore_query(_SOURCE_ID, "get_record", "invoices", {"pk_value": "001"})

    assert row is not None
    assert row["id"] == "001"
    assert row["amount"] == "500"


def test_datastore_query_over_in_memory_only_source_is_none(seeded_workspace: Path) -> None:
    """No file path was ever persisted for an in-memory-registered datastore (degrade, not crash)."""
    operator = MemoryOperator(seeded_workspace, _DID)

    result = operator.datastore_query(
        _MEM_SOURCE_ID, "get_record", "widgets", {"pk_value": "anything"}
    )

    assert result is None


def test_datastore_query_unknown_source_is_none(seeded_workspace: Path) -> None:
    operator = MemoryOperator(seeded_workspace, _DID)

    assert (
        operator.datastore_query("never-registered", "get_record", "invoices", {"pk_value": "001"})
        is None
    )
