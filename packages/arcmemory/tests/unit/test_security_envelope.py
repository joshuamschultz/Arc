"""COMP-013 (T-1044 RED / T-1045 GREEN) — brain integration + security envelope.

Every ingest/mapping/retrieval op on ``ArcMemoryBrain`` must go through one
identity+policy+audit envelope (``_guard``): a real :class:`~arctrust.policy.
PolicyPipeline` DENY blocks the op fail-closed and writes nothing; an ALLOW (or
no configured policy) proceeds and emits an audit event carrying ``caller_did``.
A committed mapping (``homes=['memory']`` vs ``['document']``) routes ingested
records to the right pool -- memory recall never sees a document-routed record
and vice versa.

RED because ``ArcMemoryBrain`` has no ``register_datastore`` / ``document_search``
/ ``datastore_query`` methods yet, and ``ingest_batch`` does not accept
``caller_did`` -- the envelope itself does not exist (AttributeError / TypeError,
not a typo).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from arctrust.audit import AuditEvent
from arctrust.policy import Decision, PolicyContext, PolicyPipeline

from arcmemory.brain import ArcMemoryBrain
from arcmemory.types import SourceRecord

_DID = "did:arc:envelope-test"


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _DenyIngestLayer:
    """A single deny-everything layer -- NOT named 'identity', so the pipeline's
    identity fast-path is skipped and every call is evaluated (and denied) here."""

    name = "deny_ingest"

    async def evaluate(self, call: object, ctx: PolicyContext) -> Decision:
        return Decision.deny(
            layer=self.name,
            rule_id="test_deny_all",
            reason="test-forced denial",
            input_hash="0" * 64,
            evaluated_at_us=0,
        )


def _episodic_count(brain: ArcMemoryBrain) -> int:
    return brain._db.connect().execute("SELECT COUNT(*) FROM episodic").fetchone()[0]


# -- DENY: fails closed, writes nothing, audits deny -------------------------


async def test_policy_deny_blocks_ingest_writes_nothing_and_audits_deny(
    workspace: Path,
) -> None:
    sink = RecordingSink()
    policy = PolicyPipeline([_DenyIngestLayer()])
    brain = ArcMemoryBrain(workspace, _DID, policy_pipeline=policy, audit_sink=sink)
    records = [SourceRecord(external_id="e1", text="the secret launch window")]

    result = await brain.ingest_batch("src-denied", records, caller_did=_DID)

    assert result.ingested == 0
    assert _episodic_count(brain) == 0
    deny_events = [e for e in sink.events if e.outcome == "deny"]
    assert deny_events, "a deny AuditEvent must be emitted when the policy denies"


# -- ALLOW: every op audits allow with caller_did -----------------------------


async def test_ingest_emits_allow_audit_carrying_caller_did(workspace: Path) -> None:
    sink = RecordingSink()
    brain = ArcMemoryBrain(workspace, _DID, audit_sink=sink)  # no policy -> allow

    await brain.ingest_batch(
        "src-allowed", [SourceRecord(external_id="e1", text="hello world")], caller_did=_DID
    )

    allow_events = [e for e in sink.events if e.outcome == "allow"]
    assert allow_events
    assert any(e.actor_did == _DID for e in allow_events)


async def test_document_search_emits_allow_audit_carrying_caller_did(workspace: Path) -> None:
    sink = RecordingSink()
    brain = ArcMemoryBrain(workspace, _DID, audit_sink=sink)

    await brain.document_search("anything", source_id="src-doc", caller_did=_DID)

    allow_events = [e for e in sink.events if e.outcome == "allow"]
    assert allow_events
    assert any(e.actor_did == _DID for e in allow_events)


async def test_datastore_query_emits_allow_audit_carrying_caller_did(workspace: Path) -> None:
    sink = RecordingSink()
    brain = ArcMemoryBrain(workspace, _DID, audit_sink=sink)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO widgets VALUES (1, 'sprocket')")
    conn.commit()
    await brain.register_sqlite_datastore("src-db", conn, caller_did=_DID)

    await brain.datastore_query("src-db", "list", "widgets", {}, caller_did=_DID)

    allow_events = [e for e in sink.events if e.outcome == "allow"]
    assert allow_events
    assert any(e.actor_did == _DID for e in allow_events)


# -- Mapping-routed ingest: memory home is recallable, document home is not --


async def test_ingest_with_memory_mapping_is_recallable_via_retrieve(workspace: Path) -> None:
    from arcmemory.db import MemoryDB
    from arcmemory.index.graph import WeightedGraph
    from arcmemory.mapping import commit_mapping
    from arcmemory.stores.semantic import SemanticStore
    from arcmemory.types import SourceMapping

    graph = WeightedGraph(MemoryDB(workspace))
    store = SemanticStore(workspace, graph, scope=_DID)
    commit_mapping(SourceMapping(source_id="src-mem", homes=["memory"]), store=store)

    brain = ArcMemoryBrain(workspace, _DID)
    await brain.ingest_batch(
        "src-mem",
        [SourceRecord(external_id="e1", text="the launch date is next friday")],
        caller_did=_DID,
    )

    text = await brain.retrieve("launch date", top_k=5, budget=10_000)

    assert "launch date" in text


async def test_ingest_with_document_mapping_indexes_to_doc_pool_not_episodic(
    workspace: Path,
) -> None:
    from arcmemory.db import MemoryDB
    from arcmemory.index.graph import WeightedGraph
    from arcmemory.mapping import commit_mapping
    from arcmemory.stores.semantic import SemanticStore
    from arcmemory.types import SourceMapping

    graph = WeightedGraph(MemoryDB(workspace))
    store = SemanticStore(workspace, graph, scope=_DID)
    commit_mapping(SourceMapping(source_id="src-doc", homes=["document"]), store=store)

    brain = ArcMemoryBrain(workspace, _DID)
    await brain.ingest_batch(
        "src-doc",
        [SourceRecord(external_id="e1", text="the quarterly compliance report body")],
        caller_did=_DID,
    )

    hits = await brain.document_search("compliance report", source_id="src-doc", caller_did=_DID)
    assert hits and any("compliance report" in h.text for h in hits)

    text = await brain.retrieve("compliance report", top_k=5, budget=10_000)
    assert "compliance report" not in text  # document-routed, never episodic


# -- No raw secret ever rides an audit payload --------------------------------


async def test_no_audit_payload_carries_a_raw_secret(workspace: Path) -> None:
    sink = RecordingSink()
    brain = ArcMemoryBrain(workspace, _DID, audit_sink=sink)
    secret = "sk-abcdefghijklmnopqrstuvwx0123456789"

    await brain.ingest_batch(
        "src-secret",
        [SourceRecord(external_id="e1", text=f"api token: {secret}")],
        caller_did=_DID,
    )

    for event in sink.events:
        dumped = event.model_dump_json()
        assert secret not in dumped, f"raw secret leaked into audit event {event.action!r}"
