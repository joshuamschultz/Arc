"""SPEC-073 Phase 4 (Polish) — COMP-014/015 (T-1050) degrade + off-switch hardening.

"Degrade, don't crash" (arcmemory/CLAUDE.md) applied to the three data-source
axes: a missing embedder still answers keyword search, an unregistered/unknown
source never raises, an unparseable classification label fails closed only at
federal, and every per-capability off-switch returns the system to its EXACT
prior behavior -- including at the write path (ingest), not merely at the read
path (search/query), which is where SPEC-073 Phase 1-3 left a gap.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from arctrust.audit import AuditEvent

from arcmemory.brain import ArcMemoryBrain
from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.doc_index import DocIndex
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.source import SourceChunk
from arcmemory.mapping import commit_mapping
from arcmemory.stores.semantic import SemanticStore
from arcmemory.sync import BackfillObject, SourceChange, SyncEngine, SyncMode
from arcmemory.types import SourceMapping, SourceRecord

_DID = "did:arc:degrade-test"


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _store(workspace: Path) -> SemanticStore:
    graph = WeightedGraph(MemoryDB(workspace))
    return SemanticStore(workspace, graph, scope=_DID)


# -- No embedder: BM25 + graph still answer, never raises -----------------------


async def test_document_search_without_embedder_still_finds_keyword_hit(
    workspace: Path,
) -> None:
    db = MemoryDB(workspace)
    db.connect()
    config = MemoryConfig()
    doc_index = DocIndex(db, workspace, config, embedder=None)
    chunk = SourceChunk(
        chunk_id="wiki:page-1#0",
        source_path="wiki:page-1",
        text="the quarterly onboarding checklist for new hires",
        classification="unclassified",
        mtime=0.0,
    )

    count = await doc_index.index_source("wiki", _DID, [chunk])
    assert count == 1

    hits = await doc_index.document_search("onboarding checklist", _DID, source_id="wiki")

    assert hits and any("onboarding checklist" in h.text for h in hits)


# -- Source offline: unregistered/unknown source never raises -------------------


async def test_datastore_query_for_unregistered_source_returns_none(workspace: Path) -> None:
    brain = ArcMemoryBrain(workspace, _DID)

    result = await brain.datastore_query(
        "never-registered", "get_record", "invoices", {"pk_value": "001"}, caller_did=_DID
    )

    assert result is None


async def test_document_search_for_unknown_source_returns_empty_list(workspace: Path) -> None:
    brain = ArcMemoryBrain(workspace, _DID)

    hits = await brain.document_search("anything", source_id="never-indexed", caller_did=_DID)

    assert hits == []


# -- No label / fail-closed: unparseable classification -------------------------


async def test_unparseable_classification_dropped_at_federal_but_readable_at_personal(
    workspace: Path,
) -> None:
    db = MemoryDB(workspace)
    db.connect()
    config = MemoryConfig()
    good = SourceChunk(
        chunk_id="wiki:p1#0",
        source_path="wiki:p1",
        text="the public rollout schedule",
        classification="unclassified",
        mtime=0.0,
    )
    bogus = SourceChunk(
        chunk_id="wiki:p2#0",
        source_path="wiki:p2",
        text="the rollout budget with a bogus label",
        classification="not-a-real-classification",
        mtime=0.0,
    )
    await DocIndex(db, workspace, config).index_source("wiki", _DID, [good, bogus])

    federal_brain = ArcMemoryBrain(workspace, _DID, config=MemoryConfig.for_tier("federal"))
    federal_hits = await federal_brain.document_search(
        "rollout", source_id="wiki", clearance="top_secret", caller_did=_DID
    )
    assert not any("bogus label" in h.text for h in federal_hits), (
        "an unparseable classification label must fail closed (dropped) at federal"
    )
    assert any("public rollout schedule" in h.text for h in federal_hits)

    personal_brain = ArcMemoryBrain(workspace, _DID, config=MemoryConfig())
    personal_hits = await personal_brain.document_search(
        "rollout", source_id="wiki", clearance="unclassified", caller_did=_DID
    )
    assert any("bogus label" in h.text for h in personal_hits), (
        "an unparseable classification label must default readable at personal tier"
    )


# -- Off-switches return the system to its EXACT prior behavior -----------------


async def test_doc_search_disabled_prevents_indexing_at_ingest_not_only_at_query(
    workspace: Path,
) -> None:
    """The off-switch must gate the WRITE path, not merely suppress the READ path.

    Before this hardening, ``document_search`` checked ``doc_search_enabled`` but
    ``ingest_batch``'s document route did not -- a batch ingested while the switch
    was off still silently populated the doc pool, findable the moment the switch
    flips back on. The off-switch must mean "not indexed", not "indexed but hidden".
    """
    commit_mapping(SourceMapping(source_id="dropbox", homes=["document"]), store=_store(workspace))
    off_brain = ArcMemoryBrain(workspace, _DID, config=MemoryConfig(doc_search_enabled=False))

    await off_brain.ingest_batch(
        "dropbox",
        [SourceRecord(external_id="d1", text="the confidential roadmap draft")],
        caller_did=_DID,
    )

    # The off-brain itself must report nothing (already guaranteed by its own toggle).
    assert await off_brain.document_search("roadmap", source_id="dropbox", caller_did=_DID) == []

    # A FRESH brain over the SAME workspace with the switch back on must find
    # NOTHING either -- proving the record was never written to the doc pool,
    # not merely hidden by the query-side guard.
    on_brain = ArcMemoryBrain(workspace, _DID, config=MemoryConfig(doc_search_enabled=True))
    hits = await on_brain.document_search("roadmap", source_id="dropbox", caller_did=_DID)
    assert hits == [], (
        "doc_search_enabled=False must prevent ingest_batch from indexing into the "
        "doc pool at all -- a record ingested while off must stay invisible even "
        "after the switch flips back on"
    )


async def test_datastore_disabled_returns_none_from_query(workspace: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE widgets (id TEXT PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO widgets VALUES ('w1', 'sprocket')")
    conn.commit()
    off_brain = ArcMemoryBrain(workspace, _DID, config=MemoryConfig(datastore_enabled=False))
    await off_brain.register_sqlite_datastore("erp", conn, caller_did=_DID)

    result = await off_brain.datastore_query(
        "erp", "get_record", "widgets", {"pk_value": "w1"}, caller_did=_DID
    )

    assert result is None


def test_source_sync_disabled_leaves_sync_engine_math_unaffected() -> None:
    """``source_sync_enabled`` gates the CALLER (a connector's trigger), never the
    engine's math -- ``SyncEngine`` reads no such field, so both configs decide
    identically. Documents that the toggle is wired off at the connector/brain
    layer, never inside this pure deterministic engine.
    """
    on_engine = SyncEngine(MemoryConfig(source_sync_enabled=True))
    off_engine = SyncEngine(MemoryConfig(source_sync_enabled=False))
    objects = [
        BackfillObject(external_id="o1", size_bytes=100, age_days=1),
        BackfillObject(external_id="o2", size_bytes=200, age_days=200),  # too old
    ]
    changes = [
        SourceChange(op="upsert", external_id="c1"),
        SourceChange(op="delete", external_id="c2"),
    ]

    assert on_engine.plan_backfill(objects) == off_engine.plan_backfill(objects)
    assert on_engine.reconcile(SyncMode.IMMUTABLE, changes) == off_engine.reconcile(
        SyncMode.IMMUTABLE, changes
    )
    # Sanity: the deterministic behavior itself is real, not a vacuous equality.
    assert on_engine.plan_backfill(objects).accepted == ["o1"]
    assert [c.op for c in on_engine.reconcile(SyncMode.IMMUTABLE, changes)] == ["upsert"]


# -- Caps + gating + audit still hold --------------------------------------------


async def test_ingest_over_cap_raises_and_writes_nothing(workspace: Path) -> None:
    config = MemoryConfig(ingest_max_batch=2)
    brain = ArcMemoryBrain(workspace, _DID, config=config)
    records = [SourceRecord(external_id=f"e{i}", text=f"event {i}") for i in range(3)]

    with pytest.raises(ValueError):
        await brain.ingest_batch("src", records, caller_did=_DID)

    db = MemoryDB(workspace)
    db.connect()
    count = db.connect().execute("SELECT COUNT(*) FROM episodic").fetchone()[0]
    assert count == 0


async def test_an_allow_audit_event_is_still_emitted_on_a_normal_ingest(workspace: Path) -> None:
    sink = RecordingSink()
    brain = ArcMemoryBrain(workspace, _DID, audit_sink=sink)

    await brain.ingest_batch(
        "src", [SourceRecord(external_id="e1", text="hello world")], caller_did=_DID
    )

    assert any(e.action == "memory.ingest" and e.outcome == "allow" for e in sink.events)
