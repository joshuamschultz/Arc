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

from pathlib import Path
from typing import Protocol, runtime_checkable

from arcokf import validate_collection_index
from arctrust.audit import AuditSink
from pydantic import BaseModel, Field

from arcmemory.collection_index import CollectionIndexStore
from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.index.backend import IndexBackend, open_index_backend
from arcmemory.index.rebuild import Embedder, embed_or_none
from arcmemory.index.source import SourceChunk
from arcmemory.index.surface import SurfaceIndex
from arcmemory.security import content_hash
from arcmemory.types import Recall, Scope


def doc_scope(agent_did: str, source_id: str) -> Scope:
    """Per-source document pool, isolated from the agent's memory-recall scope.

    ``Scope(agent_did, session_id=f'doc:{source_id}')`` yields key
    ``'<did>:doc:<source_id>'`` — distinct from the bare ``'<did>'`` recall
    scope and from every other source's doc scope.
    """
    return Scope(agent_did=agent_did, session_id=f"doc:{source_id}")


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
        """Delete exactly one object's chunks while retaining sibling objects."""
        scope = doc_scope(agent_did, source_id)
        backend = open_index_backend(self._cfg.index_backend, db=self._db)
        await backend.delete_object(scope.key, object_id)

    async def index_collection(
        self,
        source_id: str,
        agent_did: str,
        collection_root: Path,
        chunks: list[SourceChunk],
    ) -> int:
        """Commit a source inventory and index its verified routing document."""
        index_store = CollectionIndexStore(collection_root)
        self.sync_collection_index(collection_root)
        index_path = index_store.index_path
        if index_path.exists() and validate_collection_index(index_path).valid:
            index_chunk = SourceChunk(
                chunk_id=f"index:{source_id}",
                source_path=index_path.as_posix(),
                text=index_path.read_text(encoding="utf-8"),
                classification="",
                mtime=index_path.stat().st_mtime,
            )
            chunks = [*chunks, index_chunk]
        return await self.index_source(source_id, agent_did, chunks)

    def sync_collection_index(self, collection_root: Path) -> int:
        """Refresh a connected source's reserved index from its OKF inventory.

        Connector polling remains outside arcmemory; a source adapter calls this
        after it has committed its canonical documents.  The write is atomic and
        readers verify the resulting artifact before indexing it.
        """
        return CollectionIndexStore(collection_root).sync()

    async def document_search(
        self,
        query: str,
        agent_did: str,
        *,
        source_id: str | None = None,
        top_k: int = 10,
    ) -> list[DocHit]:
        """Search one source's doc pool via ``SurfaceIndex.search`` (SEARCH only).

        ``[]`` when ``doc_search_enabled`` is off or no ``source_id`` is given
        (there is no pool to search without one).
        """
        if not self._cfg.doc_search_enabled or source_id is None:
            return []
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
        result = await surface.search(query, top_k=top_k)
        hits = [await self._to_hit(backend, scope, source_id, recall) for recall in result.recalls]
        return await self._maybe_rerank(query, hits)

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
        return await embed_or_none(self._embedder, texts)

    async def _maybe_rerank(self, query: str, hits: list[DocHit]) -> list[DocHit]:
        """Bounded top-K rerank, only when injected and the top1/top2 margin is tight."""
        if self._reranker is None or len(hits) < 2:
            return hits
        margin = hits[0].score - hits[1].score
        if margin >= self._cfg.doc_rerank_margin:
            return hits
        return await self._reranker.rerank(query, hits)


__all__ = ["DocHit", "DocIndex", "Reranker", "doc_scope"]
