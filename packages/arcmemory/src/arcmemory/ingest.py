"""COMP-002 — brain-port ingestion logic (SPEC-073 Phase 1).

Kept out of ``brain.py`` so the Brain-port stays a thin delegator: the
zero-trust batch cap, content-hash dedup, and last-writer-wins ordering that
make ``ingest_batch`` correct all live here. Phase 1 writes go straight to
episodic (Phase 2 T-1027 routes memory-home writes through capture).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from arctrust.audit import AuditSink

from arcmemory.chunk import RecursiveChunker
from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.doc_index import DocIndex
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.rebuild import Embedder
from arcmemory.security import content_hash, sanitize
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Event, IngestResult, MemoryHome, Scope, SourceMapping, SourceRecord


def deterministic_event_id(source_id: str, external_id: str) -> str:
    """Stable event id for one ``(source, external_id)`` pair -- the upsert key."""
    return content_hash(f"{source_id}\x00{external_id}")


def _is_stale(existing_ts: str, incoming_ts: str) -> bool:
    """True when an incoming record must not overwrite the stored one (last-writer-wins).

    An empty incoming stamp never overwrites a *stamped* existing record. Two
    non-empty stamps compare directly; an existing record with no stamp at all
    carries no ordering information, so a stamped or unstamped incoming write
    is free to upsert it.
    """
    if incoming_ts == "":
        return existing_ts != ""
    return existing_ts != "" and existing_ts >= incoming_ts


def ingest_batch(
    db: MemoryDB,
    workspace: Path,
    scope: Scope,
    config: MemoryConfig,
    source_id: str,
    records: list[SourceRecord],
) -> IngestResult:
    """Idempotent, ordered, capped write of one pushed batch into episodic (T-1018).

    Raises ``ValueError`` -- writing nothing -- when ``records`` exceeds the
    zero-trust ``ingest_max_batch`` boundary (LLM10).
    """
    if len(records) > config.ingest_max_batch:
        raise ValueError(
            f"ingest_batch: batch of {len(records)} record(s) exceeds "
            f"ingest_max_batch={config.ingest_max_batch}"
        )
    store = EpisodicStore(db, workspace)
    result = IngestResult()
    for record in records:
        event_id = deterministic_event_id(source_id, record.external_id)
        text = sanitize(record.text, max_length=config.max_event_chars)
        digest = content_hash(text)
        existing = store.get(scope.key, event_id)
        if existing is not None:
            if existing.hash == digest:
                result.deduped += 1
                continue
            if _is_stale(existing.source_updated_at, record.source_updated_at):
                result.skipped_stale += 1
                continue
        store.append(
            Event(
                event_id=event_id,
                scope=scope.key,
                kind=record.kind,
                text=text,
                hash=digest,
                classification=record.classification,
                salience=record.salience,
                source_updated_at=record.source_updated_at,
            )
        )
        result.ingested += 1
    return result


async def route_batch(
    db: MemoryDB,
    workspace: Path,
    scope: Scope,
    config: MemoryConfig,
    source_id: str,
    records: list[SourceRecord],
    homes: Sequence[str],
    *,
    agent_did: str,
    embedder: Embedder | None = None,
    audit_sink: AuditSink | None = None,
) -> IngestResult:
    """Fan one already-capped batch to each approved home (SPEC-073 COMP-013).

    ``memory`` reuses the exact dedup/order/append :func:`ingest_batch` path
    above, so its counts are what the caller sees. ``document`` chunks each
    record's text and indexes it into the source's isolated doc pool -- it
    never touches episodic, so it is invisible to :meth:`ArcMemoryBrain.retrieve`.
    ``datastore`` is a no-op here: those rows stay live in the source's own DB,
    read through the registered :class:`~arcmemory.datastore.Datastore` instead.
    """
    result = IngestResult()
    for home in homes:
        if home == "memory":
            partial = ingest_batch(db, workspace, scope, config, source_id, records)
            result.ingested += partial.ingested
            result.deduped += partial.deduped
            result.skipped_stale += partial.skipped_stale
        elif home == "document" and config.doc_search_enabled:
            # The off-switch gates the WRITE too, not only the read: a batch ingested
            # while doc-search is disabled must not silently populate the doc pool
            # (flipping the switch back on would otherwise reveal it).
            await _index_documents(
                db, workspace, config, source_id, records, agent_did, embedder, audit_sink
            )
    return result


async def _index_documents(
    db: MemoryDB,
    workspace: Path,
    config: MemoryConfig,
    source_id: str,
    records: list[SourceRecord],
    agent_did: str,
    embedder: Embedder | None,
    audit_sink: AuditSink | None,
) -> None:
    """Chunk every record's text and index it into the source's doc pool."""
    chunker = RecursiveChunker(
        chunk_tokens=config.doc_chunk_tokens, overlap=config.doc_chunk_overlap
    )
    # Thread each record's classification onto its chunks so the no-read-up gate on
    # document_search sees the real label — an unlabeled chunk would surface a SECRET
    # document body to an unclassified clearance (the doc-axis parity of the memory gate).
    chunks = [
        chunk
        for record in records
        for chunk in chunker.chunk(
            record.text,
            source_path=f"{source_id}:{record.external_id}",
            classification=record.classification,
        )
    ]
    if not chunks:
        return
    await DocIndex(db, workspace, config, embedder=embedder, audit_sink=audit_sink).index_source(
        source_id, agent_did, chunks
    )


def register_source(
    workspace: Path, graph: WeightedGraph, scope: Scope, source_id: str, *, kind: str
) -> None:
    """Idempotently record a source's registration as a semantic Entity (T-1018)."""
    SemanticStore(workspace, graph, scope.key).write_fact(
        f"source-{source_id}", "kind", kind, entity_type="source"
    )


def propose_mapping(source_id: str, sample: list[SourceRecord] | None) -> SourceMapping:
    """Phase-1 heuristic routing: default ``memory``, widened by the sample's shape.

    Approval gating is Phase 3 (T-1039) -- this returns a proposal only.
    """
    homes = [MemoryHome.MEMORY]
    for record in sample or []:
        if record.metadata.get("mime") or record.metadata.get("path"):
            if MemoryHome.DOCUMENT not in homes:
                homes.append(MemoryHome.DOCUMENT)
        if record.metadata.get("kind") == "db_row" and MemoryHome.DATASTORE not in homes:
            homes.append(MemoryHome.DATASTORE)
    return SourceMapping(source_id=source_id, homes=homes)


__all__ = [
    "deterministic_event_id",
    "ingest_batch",
    "propose_mapping",
    "register_source",
    "route_batch",
]
