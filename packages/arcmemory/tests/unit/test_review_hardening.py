"""Regression tests for the /review blocking findings on SPEC-073.

Covers live-but-untested ingest paths (propose_mapping heuristic, register_source,
the last-writer-wins overwrite branch, the `_is_stale` empty-stamp guard), the
doc-index rerank invocation, and the new datastore no-read-up gate + tool DATA-framing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from arcmemory.brain import ArcMemoryBrain
from arcmemory.config import MemoryConfig
from arcmemory.doc_index import DocHit, DocIndex
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.source import SourceChunk
from arcmemory.ingest import _is_stale, propose_mapping, register_source
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Scope, SourceRecord

_DID = "did:arc:test-agent"


# -- ingest.propose_mapping heuristic (COMP-002) -----------------------------


def test_propose_mapping_defaults_to_memory() -> None:
    mapping = propose_mapping("s", None)
    assert mapping.homes == ["memory"]


def test_propose_mapping_adds_document_on_file_shape() -> None:
    sample = [SourceRecord(external_id="f1", text="x", metadata={"mime": "application/pdf"})]
    assert "document" in propose_mapping("s", sample).homes
    sample2 = [SourceRecord(external_id="f2", text="x", metadata={"path": "/a/b.txt"})]
    assert "document" in propose_mapping("s", sample2).homes


def test_propose_mapping_adds_datastore_on_db_row_and_dedups_homes() -> None:
    sample = [
        SourceRecord(external_id="r1", text="x", metadata={"kind": "db_row"}),
        SourceRecord(external_id="r2", text="x", metadata={"kind": "db_row", "path": "/a"}),
    ]
    homes = propose_mapping("s", sample).homes
    assert "datastore" in homes
    assert homes.count("document") <= 1  # the no-dup guard holds


# -- ingest.register_source (COMP-002) ---------------------------------------


def test_register_source_writes_a_source_entity(workspace: Path, db) -> None:
    graph = WeightedGraph(db)
    scope = Scope(agent_did=_DID)
    register_source(workspace, graph, scope, "slack", kind="chat")
    entity = SemanticStore(workspace, graph, scope.key).read("source-slack")
    assert entity is not None
    assert any(f.predicate == "kind" and f.value == "chat" for f in entity.facts)


# -- last-writer-wins overwrite branch (the newer+different-text upsert) ------


async def test_ingest_batch_newer_different_text_overwrites(workspace: Path) -> None:
    brain = ArcMemoryBrain(workspace, _DID)
    await brain.ingest_batch(
        "s", [SourceRecord(external_id="r1", text="old", source_updated_at="2026-01-01T00:00:00Z")]
    )
    result = await brain.ingest_batch(
        "s", [SourceRecord(external_id="r1", text="new", source_updated_at="2026-01-02T00:00:00Z")]
    )
    assert result.ingested == 1
    assert result.deduped == 0 and result.skipped_stale == 0
    events = EpisodicStore(brain._db, workspace).events(Scope(agent_did=_DID).key)
    assert len(events) == 1
    assert events[0].text == "new"


# -- _is_stale empty-stamp guard ---------------------------------------------


def test_is_stale_empty_incoming_blocks_only_a_stamped_existing() -> None:
    assert _is_stale("2026-01-01T00:00:00Z", "") is True  # empty never overwrites a stamped row
    assert _is_stale("", "") is False  # unstamped existing never blocks
    assert _is_stale("", "2026-01-01T00:00:00Z") is False  # newer stamp over unstamped is free


# -- doc-index rerank invocation (COMP-006) ----------------------------------


class _SpyReranker:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def rerank(self, query: str, hits: list[DocHit]) -> list[DocHit]:
        self.calls.append(query)
        return list(reversed(hits))


async def test_doc_index_reranks_when_margin_is_tight(workspace: Path, db, embedder) -> None:
    reranker = _SpyReranker()
    cfg = MemoryConfig(doc_rerank_margin=0.5)  # wide gate -> any close margin reranks
    index = DocIndex(db, workspace, cfg, embedder=embedder, reranker=reranker)
    chunks = [
        SourceChunk(
            chunk_id=f"c{i}",
            source_path=f"dropbox:{i}",
            text=f"revenue report {i}",
            classification="unclassified",
            mtime=float(i),
        )
        for i in range(3)
    ]
    await index.index_source("dropbox", _DID, chunks)
    hits = await index.document_search("revenue report", _DID, source_id="dropbox", top_k=3)
    assert reranker.calls, "a tight top1/top2 margin must invoke the injected reranker"
    assert hits  # results still returned


# -- datastore no-read-up gate (SEC-15) --------------------------------------


def _secret_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE invoices (id TEXT PRIMARY KEY, amount REAL)")
    conn.execute("INSERT INTO invoices VALUES ('001', 42.0)")
    conn.commit()
    return conn


async def test_datastore_query_gates_clearance_against_source_label(workspace: Path) -> None:
    brain = ArcMemoryBrain(workspace, _DID, config=MemoryConfig(tier="federal"))
    await brain.register_sqlite_datastore("erp", _secret_conn(), classification="secret")

    # An unclassified clearance may NOT read a secret-registered datastore.
    denied = await brain.datastore_query(
        "erp", "get_record", "invoices", {"pk_value": "001"}, clearance="unclassified"
    )
    assert denied is None

    # A dominating clearance may.
    allowed = await brain.datastore_query(
        "erp", "get_record", "invoices", {"pk_value": "001"}, clearance="secret"
    )
    assert allowed is not None
    assert allowed["id"] == "001"  # type: ignore[index]


# -- ingest.ingest_batch public zero-trust cap (independent of the brain) -----


def test_ingest_batch_module_level_cap_raises(workspace: Path, db) -> None:
    import pytest

    from arcmemory.ingest import ingest_batch

    cfg = MemoryConfig(ingest_max_batch=1)
    records = [SourceRecord(external_id=f"r{i}", text="x") for i in range(2)]
    with pytest.raises(ValueError):
        ingest_batch(db, workspace, Scope(agent_did=_DID), cfg, "s", records)
