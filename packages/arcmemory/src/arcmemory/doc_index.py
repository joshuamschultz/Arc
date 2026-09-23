"""Per-source hybrid document index + ``document_search`` (SPEC-073 COMP-006).

A connected data source gets its own **document pool** —
a scope isolated from the agent's memory-recall scope — so a search bounded to
one source can never surface another source's chunks (LLM08). Indexing stores
only the chunk's text + a pointer back to the original object; the raw file
bytes are never passed in and never stored (``SourceChunk`` carries text only).

``DocIndex`` writes through :class:`~arcmemory.index.backend.IndexBackend`
directly (never :meth:`SurfaceIndex.index_if_needed`, which would pull the
agent's own memory files into the doc pool) and reads through
:meth:`SurfaceIndex.search` — the same fused vec+bm25+graph+recency ranking
memory recall uses, scoped to exactly one source at a time.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol, runtime_checkable

from arcokf import validate_collection_index
from arctrust.audit import AuditSink
from pydantic import BaseModel, Field

from arcmemory.collection_index import CollectionIndexStore, routing_text
from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.index.backend import IndexBackend, open_index_backend
from arcmemory.index.rebuild import Embedder, embed_or_none
from arcmemory.index.source import SourceChunk, bounded_chunks
from arcmemory.index.surface import SurfaceIndex
from arcmemory.security import content_hash, dominating_classification
from arcmemory.types import Recall, Scope


def doc_scope(agent_did: str, source_id: str) -> Scope:
    """Per-source document pool, isolated from the agent's memory-recall scope.

    ``Scope(agent_did, session_id=f'doc:{source_id}')`` yields key
    ``'<did>:doc:<source_id>'`` — distinct from the bare ``'<did>'`` recall
    scope and from every other source's doc scope.
    """
    return Scope(agent_did=agent_did, session_id=f"doc:{source_id}")


def object_key(source_id: str, object_id: str) -> str:
    """The chunk-id stem of one source object: ``<source_id>:<object_id>``.

    The SQLite ``chunks`` table (and ``vec0``) key rows by chunk id alone, so a
    bare object id let two sources that share one (the same path in two
    accounts) overwrite each other's rows. Pushed-record ingest already
    qualifies its chunk ids the same way.
    """
    return f"{source_id}:{object_id}"


class DocHit(BaseModel):
    """One document-search result: CHUNK text + a pointer, never file bytes."""

    chunk_id: str
    text: str
    pointer: str
    source_id: str
    score: float
    classification: str = "unclassified"
    provenance: list[str] = Field(default_factory=list)


@runtime_checkable
class Reranker(Protocol):
    """Optional bounded top-K rerank seam, invoked only on a tight score margin."""

    async def rerank(self, query: str, hits: list[DocHit]) -> list[DocHit]: ...


class DocIndex:
    """Write (``index_source``) and read (``document_search``) one agent's doc pools."""

    def __init__(
        self,
        db: MemoryDB,
        workspace: Path,
        config: MemoryConfig,
        *,
        embedder: Embedder | None = None,
        audit_sink: AuditSink | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        self._db = db
        self._workspace = Path(workspace)
        self._cfg = config
        self._embedder = embedder
        self._audit = audit_sink
        self._reranker = reranker

    async def index_source(self, source_id: str, agent_did: str, chunks: list[SourceChunk]) -> int:
        """Upsert every chunk under this source's doc-scope; return count indexed."""
        scope = doc_scope(agent_did, source_id)
        backend = open_index_backend(self._cfg.index_backend, db=self._db)
        embeddings = await self._embed(backend, [c.text for c in chunks])
        for i, chunk in enumerate(chunks):
            await backend.upsert_chunk(
                scope=scope.key,
                chunk_id=chunk.chunk_id,
                source_path=chunk.source_path,
                mtime=chunk.mtime,
                classification=chunk.classification,
                content_hash=content_hash(chunk.text),
                text=chunk.text,
                embedding=embeddings[i] if embeddings is not None else None,
            )
        return len(chunks)

    async def delete_object(self, source_id: str, agent_did: str, object_id: str) -> None:
        """Delete exactly one object's chunks while retaining sibling objects.

        Chunks written before ids were source-qualified carry the bare object
        id; they are removed with the object too, so a changed object never
        leaves a stale copy behind in its source's pool.
        """
        scope = doc_scope(agent_did, source_id)
        backend = open_index_backend(self._cfg.index_backend, db=self._db)
        await backend.delete_object(scope.key, object_key(source_id, object_id))
        await backend.delete_object(scope.key, object_id)

    async def delete_source(self, source_id: str, agent_did: str) -> None:
        """Delete every indexed chunk in one connected source's isolated pool."""
        backend = open_index_backend(self._cfg.index_backend, db=self._db)
        await backend.delete_scope(doc_scope(agent_did, source_id).key)

    async def refresh_collection_index(
        self, source_id: str, agent_did: str, collection_root: Path
    ) -> int:
        """Bring one source's routing ``index.md`` up to date and re-index it.

        Run once per sync run, never per object: the index lists the whole
        source, so maintaining it per object made every ingest cost the size of
        the inventory. The on-disk index is trusted for an incremental refresh
        only when its routing text is exactly what was last indexed; anything
        else (first run, a crash, an edit behind our back) is rebuilt from the
        documents. The result is validated again before it is searchable, and
        indexed as bounded windows labelled with the source's dominating
        classification. File work runs off the event loop. Returns the entry
        count.
        """
        scope = doc_scope(agent_did, source_id).key
        backend = open_index_backend(self._cfg.index_backend, db=self._db)
        store = CollectionIndexStore(collection_root)
        base_id = f"index:{source_id}"
        indexed = await _indexed_windows(backend, scope, base_id)
        on_disk = await asyncio.to_thread(_routing_windows, store.index_path, base_id)
        trusted = on_disk is not None and [chunk.text for chunk in on_disk] == indexed
        count = await asyncio.to_thread(store.refresh if trusted else store.sync)
        fresh = await asyncio.to_thread(_routing_windows, store.index_path, base_id)
        await self._replace_index_windows(source_id, agent_did, fresh or [], indexed)
        return count

    async def _replace_index_windows(
        self, source_id: str, agent_did: str, windows: list[SourceChunk], indexed: list[str]
    ) -> None:
        """Swap the indexed routing windows, skipping the embed when nothing changed."""
        scope = doc_scope(agent_did, source_id).key
        backend = open_index_backend(self._cfg.index_backend, db=self._db)
        base_id = f"index:{source_id}"
        label = dominating_classification(sorted(await backend.scope_classifications(scope)))
        meta = await backend.chunk_meta(scope, base_id)
        unchanged = [chunk.text for chunk in windows] == indexed
        if unchanged and (not windows or (meta is not None and meta[1] == label)):
            return
        await backend.delete_object(scope, base_id)
        if windows:
            labelled = [chunk.model_copy(update={"classification": label}) for chunk in windows]
            await self.index_source(source_id, agent_did, labelled)

    async def document_search(
        self,
        query: str,
        agent_did: str,
        *,
        source_id: str | None = None,
        top_k: int | None = None,
    ) -> list[DocHit]:
        """Search one source's doc pool via ``SurfaceIndex.search`` (SEARCH only).

        ``top_k`` falls back to the operator's ``doc_search_top_k`` setting when
        the caller omits it, and hits below ``doc_search_min_score`` are dropped.
        ``[]`` when ``doc_search_enabled`` is off or no ``source_id`` is given
        (there is no pool to search without one).
        """
        if not self._cfg.doc_search_enabled or source_id is None:
            return []
        k = self._cfg.doc_search_top_k if top_k is None else top_k
        scope = doc_scope(agent_did, source_id)
        backend = open_index_backend(self._cfg.index_backend, db=self._db)
        surface = SurfaceIndex(
            self._db,
            self._workspace,
            scope,
            config=self._cfg,
            embedder=self._embedder,
            audit_sink=self._audit,
        )
        result = await surface.search(query, top_k=k)
        floor = self._cfg.doc_search_min_score
        recalls = [recall for recall in result.recalls if recall.score >= floor]
        hits = [await self._to_hit(backend, scope, source_id, recall) for recall in recalls]
        return await self._maybe_rerank(query, hits)

    async def list_documents(
        self, agent_did: str, *, source_id: str, limit: int = 50
    ) -> list[DocHit]:
        """The documents indexed for one source, newest first, without a query.

        Search alone left an operator guessing: the panel could only answer a
        question, so a source that had indexed perfectly well looked empty until
        someone typed the right word. One entry per document rather than per
        chunk, because a document is the thing a person is looking for.
        """
        if not self._cfg.doc_search_enabled or not source_id:
            return []
        scope = doc_scope(agent_did, source_id)
        backend = open_index_backend(self._cfg.index_backend, db=self._db)
        hits: list[DocHit] = []
        seen: set[str] = set()
        for chunk_id in await backend.recency_order(scope.key):
            meta = await backend.chunk_meta(scope.key, chunk_id)
            pointer = meta[0] if meta is not None else ""
            if pointer in seen:
                continue
            seen.add(pointer)
            hits.append(
                DocHit(
                    chunk_id=chunk_id,
                    text=(await backend.chunk_text(scope.key, chunk_id)) or "",
                    pointer=pointer,
                    source_id=source_id,
                    score=0.0,
                    classification=meta[1] if meta is not None else "unclassified",
                    provenance=[source_id],
                )
            )
            if len(hits) >= limit:
                break
        return hits

    async def _to_hit(
        self, backend: IndexBackend, scope: Scope, source_id: str, recall: Recall
    ) -> DocHit:
        """Hydrate one fused ``Recall`` into a ``DocHit`` (pointer via chunk_meta)."""
        meta = await backend.chunk_meta(scope.key, recall.source)
        pointer = meta[0] if meta is not None else ""
        return DocHit(
            chunk_id=recall.source,
            text=recall.content,
            pointer=pointer,
            source_id=source_id,
            score=recall.score,
            classification=recall.classification,
            provenance=[source_id],
        )

    async def _embed(self, backend: IndexBackend, texts: list[str]) -> list[list[float]] | None:
        """Embed through the injected seam, or ``None`` when embeddings are unavailable."""
        if not backend.vec_available:
            return None
        return await embed_or_none(self._embedder, texts, operation="embed:ingest")

    async def _maybe_rerank(self, query: str, hits: list[DocHit]) -> list[DocHit]:
        """Bounded top-K rerank, only when injected and the top1/top2 margin is tight."""
        if self._reranker is None or len(hits) < 2:
            return hits
        margin = hits[0].score - hits[1].score
        if margin >= self._cfg.doc_rerank_margin:
            return hits
        return await self._reranker.rerank(query, hits)


async def _indexed_windows(backend: IndexBackend, scope: str, base_id: str) -> list[str]:
    """The routing-index window texts currently searchable, in window order."""
    texts: list[str] = []
    while True:
        chunk_id = base_id if not texts else f"{base_id}#{len(texts)}"
        text = await backend.chunk_text(scope, chunk_id)
        if text is None:
            return texts
        texts.append(text)


def _routing_windows(index_path: Path, base_id: str) -> list[SourceChunk] | None:
    """Bounded windows of a verified index's routing lines; ``None`` if untrusted.

    An index that lists nothing yields no windows, so an emptied source stops
    surfacing a bare heading.
    """
    validation = validate_collection_index(index_path)
    if not validation.valid:
        return None
    if not validation.entries:
        return []
    return list(
        bounded_chunks(
            base_id,
            index_path.as_posix(),
            routing_text(index_path.read_text(encoding="utf-8")),
            "",
            index_path.stat().st_mtime,
        )
    )


__all__ = ["DocHit", "DocIndex", "Reranker", "doc_scope", "object_key"]
