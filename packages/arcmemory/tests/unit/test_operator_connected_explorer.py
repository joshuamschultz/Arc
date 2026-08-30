"""H-024 — MemoryOperator connected-data EXPLORER: chunk browse/search and table
schema BOUND to one connection's source-scope.

H-023 gave ``browse_chunks``/``search_chunks`` over the agent's *recall* scope.
H-024 adds an additive ``source_id=`` that re-points them at exactly one
connected source's document pool (``doc_scope`` = ``"<did>:doc:<source_id>"``),
so the explorer shows only that connection's chunks — gated no-read-up BEFORE the
page slice, degrading LOUD on vector, with no cross-source or cross-agent leak and
no way to infer an over-clearance chunk's existence from a shifted count.

It also adds an additive ``source_id=`` to ``list_datastore_tables`` so the table
schema shown is exactly that connection's, read from the PERSISTED ontology (no
live DB connection needed — it works with the backing datastore unreachable).

Every fixture is seeded through arcmemory's own primitives (``ArcMemoryBrain.
capture``, ``DocIndex.index_source``, ``ArcMemoryBrain.register_sqlite_datastore``)
— never hand-inserted SQL.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from arcmemory.brain import ArcMemoryBrain
from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.doc_index import DocIndex
from arcmemory.index.source import SourceChunk
from arcmemory.operator import ChunkPage, ChunkSearchResult, MemoryOperator

_DID = "did:arc:explorer-agent"
_OTHER_DID = "did:arc:other-agent"
_SOURCE_A = "acct-a"
_SOURCE_B = "acct-b"


def _operator(workspace: Path, did: str = _DID) -> MemoryOperator:
    return MemoryOperator(workspace, did)


async def _index_doc_chunk(
    workspace: Path,
    source_id: str,
    chunk_id: str,
    text: str,
    *,
    classification: str = "unclassified",
    mtime: float = 0.0,
    did: str = _DID,
) -> None:
    cfg = MemoryConfig()
    db = MemoryDB(workspace)
    await DocIndex(db, workspace, cfg).index_source(
        source_id,
        did,
        [
            SourceChunk(
                chunk_id=chunk_id,
                source_path=f"{source_id}:{chunk_id}",
                text=text,
                classification=classification,
                mtime=mtime,
            )
        ],
    )


async def _seed_two_sources(workspace: Path) -> None:
    """Recall-scope capture + two connected sources' doc pools (one SECRET chunk)."""
    brain = ArcMemoryBrain(workspace, _DID)
    await brain.capture("recall-scope memory about widgets", kind="observation")

    await _index_doc_chunk(workspace, _SOURCE_A, "a-public", "acme invoice public total")
    await _index_doc_chunk(
        workspace, _SOURCE_A, "a-secret", "acme launch codes secret", classification="SECRET"
    )
    await _index_doc_chunk(workspace, _SOURCE_B, "b-only", "beta corp shipment record")


# -- browse_chunks bound to a source ------------------------------------------


async def test_browse_chunks_source_returns_only_that_sources_chunks(workspace: Path) -> None:
    await _seed_two_sources(workspace)
    page = await _operator(workspace).browse_chunks(source_id=_SOURCE_A, clearance="SECRET")

    assert isinstance(page, ChunkPage)
    texts = " ".join(item.text for item in page.items)
    assert "acme" in texts
    assert "beta corp" not in texts  # no cross-source leak
    assert "recall-scope memory" not in texts  # no recall-scope leak
    assert page.total == 2


async def test_browse_chunks_source_gate_before_slice_hides_count(workspace: Path) -> None:
    await _seed_two_sources(workspace)
    page = await _operator(workspace).browse_chunks(source_id=_SOURCE_A, clearance="unclassified")

    # The SECRET chunk is gated out BEFORE the slice: total reflects only the
    # visible chunk, so its very existence can't be inferred from the count.
    assert page.total == 1
    assert all("launch codes" not in item.text for item in page.items)
    assert all(item.classification != "SECRET" for item in page.items)


async def test_browse_chunks_source_caps_text(workspace: Path) -> None:
    await _index_doc_chunk(workspace, _SOURCE_A, "a-long", "widget " * 400)
    page = await _operator(workspace).browse_chunks(source_id=_SOURCE_A)

    assert page.items
    assert all(len(item.text) <= 500 for item in page.items)
    assert page.items[0].truncated is True


async def test_browse_chunks_without_source_still_uses_recall_scope(workspace: Path) -> None:
    await _seed_two_sources(workspace)
    page = await _operator(workspace).browse_chunks()  # additive: unchanged default

    texts = " ".join(item.text for item in page.items)
    assert "recall-scope memory" in texts
    assert "acme" not in texts  # recall scope never sees the doc pools


# -- search_chunks bound to a source ------------------------------------------


async def test_search_chunks_source_literal_scoped(workspace: Path) -> None:
    await _seed_two_sources(workspace)
    result = await _operator(workspace).search_chunks(
        "acme", mode="literal", source_id=_SOURCE_A, clearance="SECRET"
    )

    assert isinstance(result, ChunkSearchResult)
    assert result.mode == "literal"
    assert result.degraded is False
    assert result.items and all("beta corp" not in item.text for item in result.items)


async def test_search_chunks_source_excludes_over_clearance(workspace: Path) -> None:
    await _seed_two_sources(workspace)
    result = await _operator(workspace).search_chunks(
        "acme", mode="literal", source_id=_SOURCE_A, clearance="unclassified"
    )
    assert all("launch codes" not in item.text for item in result.items)
    assert all(item.classification != "SECRET" for item in result.items)


async def test_search_chunks_source_vector_degrades_loud_without_embedder(
    workspace: Path,
) -> None:
    await _seed_two_sources(workspace)
    result = await _operator(workspace).search_chunks(
        "acme", mode="vector", source_id=_SOURCE_A, clearance="SECRET"
    )
    assert result.degraded is True
    assert result.mode == "literal"  # fell back, never returned a misleading empty


# -- cross-agent isolation (the scope key's DID half is the operator's own) ----


async def test_browse_chunks_source_cross_agent_cannot_reach_other_dids_pool(
    workspace: Path,
) -> None:
    await _seed_two_sources(workspace)  # seeded under _DID

    # A different agent, same workspace path, same source_id string: its doc scope
    # is "<_OTHER_DID>:doc:acct-a", never "<_DID>:doc:acct-a", so it sees nothing.
    page = await _operator(workspace, _OTHER_DID).browse_chunks(
        source_id=_SOURCE_A, clearance="SECRET"
    )
    assert page.total == 0
    assert page.items == []


# -- list_datastore_tables bound to a source ----------------------------------


async def _register_datastore(workspace: Path, source_id: str, table: str) -> None:
    brain = ArcMemoryBrain(workspace, _DID)
    conn = sqlite3.connect(":memory:")
    conn.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY, amount TEXT)")
    conn.commit()
    await brain.register_sqlite_datastore(source_id, conn)


async def test_list_datastore_tables_scoped_to_source(workspace: Path) -> None:
    await _register_datastore(workspace, _SOURCE_A, "invoices")

    tables = _operator(workspace).list_datastore_tables(source_id=_SOURCE_A)

    assert tables
    assert all(t.slug.startswith(f"db-table-{_SOURCE_A}-") for t in tables)
    assert any(t.name == "invoices" for t in tables)


async def test_list_datastore_tables_scoped_to_unknown_source_is_empty(workspace: Path) -> None:
    await _register_datastore(workspace, _SOURCE_A, "invoices")

    assert _operator(workspace).list_datastore_tables(source_id="never-registered") == []


async def test_list_datastore_tables_unscoped_returns_all(workspace: Path) -> None:
    await _register_datastore(workspace, _SOURCE_A, "invoices")

    tables = _operator(workspace).list_datastore_tables()
    assert any(t.name == "invoices" for t in tables)
