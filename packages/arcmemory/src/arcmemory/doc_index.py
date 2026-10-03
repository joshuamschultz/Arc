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
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from arctrust.audit import AuditSink
from pydantic import BaseModel, Field

from arcmemory.collection_index import memory_maintainer, routing_text, source_maintainer
from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.index.backend import ChunkWrite, IndexBackend, open_index_backend
from arcmemory.index.rebuild import Embedder, embed_or_none
from arcmemory.index.source import SourceChunk, bounded_chunks
from arcmemory.index.surface import SurfaceIndex
from arcmemory.mdfile import read_frontmatter
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
    #: Citation fields, read from the extracted document's front matter. They
    #: are source-supplied text, so the renderer frames them as DATA too.
    title: str = ""
    source_kind: str = ""
    url: str = ""
    updated_at: str = ""


#: Chunks fetched per pool, as a multiple of the requested hit count, so that
#: collapsing several chunks of one long document still leaves ``top_k`` documents.
_COLLAPSE_OVERFETCH = 4
#: Document-pool fusion. Questions are paraphrases of a page, not copies, so the
#: vector channel counts double, and a small RRF constant lets a channel's top
#: ranks decide the order instead of a flat sum that rewards incidental word overlap.
_DOC_VECTOR_WEIGHT = 2
_DOC_RRF_K = 5
_CITATION_FIELD_CAP = 300
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _citation_text(value: Any) -> str:
    """One bounded, single-line citation value (no control characters)."""
    return _CONTROL_CHARS.sub(" ", str(value or "")).strip()[:_CITATION_FIELD_CAP]


def _citation_frontmatter(path: Path) -> dict[str, Any] | None:
    """A document's front matter, or ``None`` for a file that is not a valid OKF document.

    The collection ``index.md`` lives beside the documents and is a hit too; it has
    no citation of its own.
    """
    try:
        return read_frontmatter(path)
    except ValueError:
        return None


def _best_chunk_per_document(hits: list[DocHit]) -> list[DocHit]:
    """Keep each document's highest-scoring chunk, best document first."""
    best: dict[tuple[str, str], DocHit] = {}
    for hit in hits:
        key = (hit.source_id, hit.pointer)
        current = best.get(key)
        if current is None or hit.score > current.score:
            best[key] = hit
    return sorted(best.values(), key=lambda hit: hit.score, reverse=True)


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
        await backend.upsert_chunks(
            scope.key,
            [
                ChunkWrite(
                    chunk_id=chunk.chunk_id,
                    source_path=chunk.source_path,
                    mtime=chunk.mtime,
                    classification=chunk.classification,
                    content_hash=content_hash(chunk.text),
                    text=chunk.text,
                    embedding=embeddings[i] if embeddings is not None else None,
                )
                for i, chunk in enumerate(chunks)
            ],
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

    async def repath_object(
        self, source_id: str, agent_did: str, object_id: str, source_path: str
    ) -> int:
        """Point one object's chunks at the file's new path without re-embedding.

        Chunk ids are keyed by the object, not its location, so a document that
        moves keeps every chunk, text and vector; only the stored pointer
        changes. Returns the number of chunks updated (0 when already current).
        """
        scope = doc_scope(agent_did, source_id)
        backend = open_index_backend(self._cfg.index_backend, db=self._db)
        return await backend.repath_object(
            scope.key, object_key(source_id, object_id), source_path
        )

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
        the inventory. The source's maintainer heals only what is missing,
        tampered or older than a document (a ``stat`` pass; nothing is re-read
        when the folder is current), the connected-sources listing above it is
        marked for refresh, and the verified routing lines are indexed as
        bounded windows labelled with the source's dominating classification.
        File work runs off the event loop. Returns the number of listed entries.
        """
        scope = doc_scope(agent_did, source_id).key
        backend = open_index_backend(self._cfg.index_backend, db=self._db)
        maintainer = source_maintainer(collection_root)
        base_id = f"index:{source_id}"
        indexed = await _indexed_windows(backend, scope, base_id)
        # The on-disk index is trusted for an incremental refresh only when its
        # routing text is exactly what was last indexed. Anything else (first
        # run, a crash, an edit behind our back, even one forged together with
        # its digest sidecar) is rebuilt from the documents.
        on_disk = await asyncio.to_thread(_routing_windows, collection_root, base_id)
        trusted = on_disk is not None and [chunk.text for chunk in on_disk] == indexed
        await asyncio.to_thread(maintainer.sync_all, force=not trusted)
        memory_maintainer(self._workspace / "memory").mark_folder_dirty(collection_root.parent)
        fresh = await asyncio.to_thread(_routing_windows, collection_root, base_id)
        await self._replace_index_windows(source_id, agent_did, fresh or [], indexed)
        return len(fresh or [])

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
        source_ids: Sequence[str] | None = None,
        top_k: int | None = None,
    ) -> list[DocHit]:
        """Search document pools via ``SurfaceIndex.search`` (SEARCH only).

        With no ``source_id``/``source_ids`` it fans out across every pool this
        agent owns; a pool exists only for a source whose mapping an operator
        approved, and purging a source deletes its pool. Hits are collapsed to
        the best chunk per document, so one long document cannot fill ``top_k``.
        ``top_k`` falls back to the operator's ``doc_search_top_k`` setting and
        hits below ``doc_search_min_score`` are dropped. ``[]`` when
        ``doc_search_enabled`` is off.
        """
        if not self._cfg.doc_search_enabled:
            return []
        k = self._cfg.doc_search_top_k if top_k is None else top_k
        wanted = [source_id] if source_id is not None else source_ids
        hits: list[DocHit] = []
        for pool in await self._pool_ids(agent_did, wanted):
            hits.extend(await self._search_pool(query, agent_did, pool, k * _COLLAPSE_OVERFETCH))
        collapsed = _best_chunk_per_document(hits)[:k]
        reranked = await self._maybe_rerank(query, collapsed)
        return await asyncio.to_thread(self._with_citations, reranked)

    async def _pool_ids(self, agent_did: str, wanted: Sequence[str] | None) -> list[str]:
        """The source ids to search: the requested ones, or every pool this agent owns."""
        if wanted is not None:
            return list(dict.fromkeys(wanted))
        backend = open_index_backend(self._cfg.index_backend, db=self._db)
        prefix = doc_scope(agent_did, "").key
        return [scope[len(prefix) :] for scope in await backend.scopes_with_prefix(prefix)]

    async def _search_pool(
        self, query: str, agent_did: str, source_id: str, limit: int
    ) -> list[DocHit]:
        """Fused search of exactly one source's pool, floor-filtered and hydrated."""
        scope = doc_scope(agent_did, source_id)
        backend = open_index_backend(self._cfg.index_backend, db=self._db)
        surface = SurfaceIndex(
            self._db,
            self._workspace,
            scope,
            config=self._cfg,
            embedder=self._embedder,
            audit_sink=self._audit,
            use_recency=False,
            vector_weight=_DOC_VECTOR_WEIGHT,
            rrf_k=_DOC_RRF_K,
        )
        result = await surface.search(query, top_k=limit)
        floor = self._cfg.doc_search_min_score
        recalls = [recall for recall in result.recalls if recall.score >= floor]
        return [await self._to_hit(backend, scope, source_id, recall) for recall in recalls]

    def _with_citations(self, hits: list[DocHit]) -> list[DocHit]:
        """Attach title, kind, url and updated time from each document's front matter."""
        root = (self._workspace / "memory" / "connected").resolve()
        cited: list[DocHit] = []
        for hit in hits:
            path = (self._workspace / hit.pointer).resolve()
            frontmatter = _citation_frontmatter(path) if path.is_relative_to(root) else None
            if not frontmatter:
                cited.append(hit)
                continue
            cited.append(
                hit.model_copy(
                    update={
                        "title": _citation_text(frontmatter.get("title")),
                        "source_kind": _citation_text(frontmatter.get("source_kind")),
                        "url": _citation_text(
                            frontmatter.get("url") or frontmatter.get("locator")
                        ),
                        "updated_at": _citation_text(frontmatter.get("updated_at")),
                    }
                )
            )
        return cited

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


def _routing_windows(collection_root: Path, base_id: str) -> list[SourceChunk] | None:
    """Bounded windows of a verified index's routing lines; ``None`` if untrusted.

    Verification is the O(1) sidecar check plus the agent's signed seal: the
    index must be canonical, match its digest, and that digest must be one the
    agent signed. The windows are cut from the exact verified text, never a
    second read. An index that lists nothing yields no windows, so an emptied
    source stops surfacing a bare heading.
    """
    validation = source_maintainer(collection_root).validate()
    if not validation.valid:
        return None
    if not validation.entries:
        return []
    index_path = collection_root / "index.md"
    return list(
        bounded_chunks(
            base_id,
            index_path.as_posix(),
            routing_text(validation.text),
            "",
            index_path.lstat().st_mtime,
        )
    )


__all__ = ["DocHit", "DocIndex", "Reranker", "doc_scope", "object_key"]
