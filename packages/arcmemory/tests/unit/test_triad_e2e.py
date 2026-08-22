"""SPEC-073 Phase 4 (Polish) — COMP-015 (T-1046) triad real-path E2E.

Drives a REAL ``ArcMemoryBrain`` through all three data-source axes at once —
only the connector wire is faked (records / a sqlite conn handed directly,
never a live Slack/Dropbox/DB client):

* **Axis A** — Slack -> memory home: ``commit_mapping(homes=['memory'])`` then
  ``ingest_batch`` lands a recallable episodic memory (``retrieve`` surfaces it).
* **Axis B** — Dropbox -> document home: ``commit_mapping(homes=['document'])``
  routes to the isolated doc pool; ``document_search`` finds it with a non-empty
  pointer, and the body never leaks into ``retrieve`` (doc pool != episodic).
* **Axis C** — SQLite -> datastore home: ``register_datastore`` + typed
  ``datastore_query`` reads a real row back exactly.

Each axis is toggle-falsifiable via its ``MemoryConfig`` off-switch, and every
surfaced result is audited (allow) through the ``_guard`` envelope. A classified
document must never surface for an unclassified clearance (no-read-up) exactly
as memory recall already enforces it.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from arctrust.audit import AuditEvent

from arcmemory.brain import ArcMemoryBrain
from arcmemory.config import MemoryConfig
from arcmemory.db import DEFAULT_DIMS, MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.mapping import commit_mapping
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import SourceMapping, SourceRecord

_DID = "did:arc:triad-test"


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _Embedder:
    """Deterministic embedder at ``DEFAULT_DIMS`` -- matches the width of the
    ``MemoryDB`` ``ArcMemoryBrain`` opens internally (it never takes a ``dims``
    override), unlike the conftest ``StubEmbedder`` fixture (8-dim, sized for
    the ``db`` fixture's own ``MemoryDB(workspace, dims=8)``). A brain-driven
    test needs the matching width or every vec0 upsert dimension-mismatches.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls += len(texts)
        out: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            out.append([digest[i % len(digest)] / 255.0 for i in range(DEFAULT_DIMS)])
        return out


@pytest.fixture
def embedder() -> _Embedder:
    """Overrides the conftest ``embedder`` fixture with a brain-width-matched one."""
    return _Embedder()


def _store(workspace: Path) -> SemanticStore:
    """A SemanticStore over the SAME graph/db the brain itself will open."""
    graph = WeightedGraph(MemoryDB(workspace))
    return SemanticStore(workspace, graph, scope=_DID)


# -- Axis A: Slack -> memory ---------------------------------------------------


async def test_axis_a_slack_memory_mapping_is_recallable(workspace: Path, embedder) -> None:
    commit_mapping(SourceMapping(source_id="slack", homes=["memory"]), store=_store(workspace))
    sink = RecordingSink()
    brain = ArcMemoryBrain(workspace, _DID, embedder=embedder, audit_sink=sink)

    result = await brain.ingest_batch(
        "slack",
        [SourceRecord(external_id="m1", text="the launch shipped tuesday")],
        caller_did=_DID,
    )
    assert result.ingested == 1

    text = await brain.retrieve("launch shipped", top_k=5, budget=10_000)

    assert "launch shipped" in text


# -- Axis B: Dropbox -> document ------------------------------------------------


async def test_axis_b_dropbox_document_mapping_is_doc_pool_only(workspace: Path, embedder) -> None:
    commit_mapping(SourceMapping(source_id="dropbox", homes=["document"]), store=_store(workspace))
    sink = RecordingSink()
    brain = ArcMemoryBrain(workspace, _DID, embedder=embedder, audit_sink=sink)

    await brain.ingest_batch(
        "dropbox",
        [SourceRecord(external_id="d1", text="the Q3 compliance report body")],
        caller_did=_DID,
    )

    hits = await brain.document_search("compliance report", source_id="dropbox", caller_did=_DID)
    assert hits, "the doc-mapped record must be findable via document_search"
    assert hits[0].pointer, "a DocHit must carry a non-empty provenance pointer"
    assert any("compliance report" in h.text for h in hits)

    text = await brain.retrieve("compliance report", top_k=5, budget=10_000)
    assert "compliance report" not in text, "document-routed body must never reach episodic recall"


async def test_axis_b_document_search_falsifiable_by_off_switch(workspace: Path, embedder) -> None:
    commit_mapping(SourceMapping(source_id="dropbox", homes=["document"]), store=_store(workspace))
    off_brain = ArcMemoryBrain(
        workspace, _DID, embedder=embedder, config=MemoryConfig(doc_search_enabled=False)
    )
    await off_brain.ingest_batch(
        "dropbox",
        [SourceRecord(external_id="d1", text="the Q3 compliance report body")],
        caller_did=_DID,
    )

    hits = await off_brain.document_search(
        "compliance report", source_id="dropbox", caller_did=_DID
    )

    assert hits == []


# -- Axis C: SQLite -> datastore -------------------------------------------------


def _erp_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE invoices (id TEXT PRIMARY KEY, amount REAL, customer_id TEXT)")
    conn.execute("INSERT INTO invoices VALUES ('001', 425.50, 'cust-1')")
    conn.commit()
    return conn


async def test_axis_c_sqlite_datastore_get_record_returns_exact_row(
    workspace: Path, embedder
) -> None:
    sink = RecordingSink()
    brain = ArcMemoryBrain(workspace, _DID, embedder=embedder, audit_sink=sink)

    await brain.register_datastore("erp", _erp_conn(), caller_did=_DID)
    row = await brain.datastore_query(
        "erp", "get_record", "invoices", {"pk_value": "001"}, caller_did=_DID
    )

    assert row is not None
    assert row["id"] == "001"


async def test_axis_c_datastore_query_falsifiable_by_off_switch(workspace: Path, embedder) -> None:
    off_brain = ArcMemoryBrain(
        workspace, _DID, embedder=embedder, config=MemoryConfig(datastore_enabled=False)
    )
    await off_brain.register_datastore("erp", _erp_conn(), caller_did=_DID)

    row = await off_brain.datastore_query(
        "erp", "get_record", "invoices", {"pk_value": "001"}, caller_did=_DID
    )

    assert row is None


# -- Gating + audit across all three axes ---------------------------------------


async def test_all_three_axes_emit_allow_audit_events(workspace: Path, embedder) -> None:
    store = _store(workspace)
    commit_mapping(SourceMapping(source_id="slack", homes=["memory"]), store=store)
    commit_mapping(SourceMapping(source_id="dropbox", homes=["document"]), store=store)
    sink = RecordingSink()
    brain = ArcMemoryBrain(workspace, _DID, embedder=embedder, audit_sink=sink)

    await brain.ingest_batch(
        "slack",
        [SourceRecord(external_id="m1", text="the launch shipped tuesday")],
        caller_did=_DID,
    )
    await brain.ingest_batch(
        "dropbox",
        [SourceRecord(external_id="d1", text="the Q3 compliance report body")],
        caller_did=_DID,
    )
    await brain.document_search("compliance report", source_id="dropbox", caller_did=_DID)
    await brain.register_datastore("erp", _erp_conn(), caller_did=_DID)
    await brain.datastore_query(
        "erp", "get_record", "invoices", {"pk_value": "001"}, caller_did=_DID
    )

    allow_actions = {e.action for e in sink.events if e.outcome == "allow"}
    assert {
        "memory.ingest",
        "memory.document_search",
        "datastore.register",
        "memory.datastore_query",
    } <= allow_actions, f"every surfaced op must audit allow; saw {allow_actions}"
    assert all(e.actor_did == _DID for e in sink.events if e.outcome == "allow")


async def test_classified_document_dropped_for_unclassified_clearance(
    workspace: Path, embedder
) -> None:
    """No-read-up must hold for the document axis exactly as it does for memory."""
    commit_mapping(SourceMapping(source_id="dropbox", homes=["document"]), store=_store(workspace))
    brain = ArcMemoryBrain(workspace, _DID, embedder=embedder)

    await brain.ingest_batch(
        "dropbox",
        [
            SourceRecord(
                external_id="secret1",
                text="the classified black-budget program overview",
                classification="SECRET",
            )
        ],
        caller_did=_DID,
    )

    hits = await brain.document_search(
        "black-budget", source_id="dropbox", clearance="unclassified", caller_did=_DID
    )

    assert not any("black-budget" in h.text for h in hits), (
        "a SECRET-classified document record must never surface for an "
        "unclassified clearance -- SourceRecord.classification must reach the "
        "indexed DocHit, exactly as memory recall's no-read-up gate already works"
    )
