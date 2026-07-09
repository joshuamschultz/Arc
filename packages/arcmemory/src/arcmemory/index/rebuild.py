"""Index rebuild — re-derive every disposable index from truth (REQ-022, ASI06).

The glass-box markdown files + the raw episodic stream are the source of truth; the
``fts_chunks``, ``vec0``, and ``edges`` tables are a disposable cache. ``rebuild``
wipes them and re-derives them deterministically, so a corrupted or poisoned index
is fixed by ``wipe -> rebuild`` and the result is byte-identical to the last good one.

The vector step embeds through an **injected** seam (``Embedder``) rather than
reaching for arcllm directly: production passes an arcllm-backed adapter, tests pass
a deterministic stub. When no embedder (or no ``sqlite-vec``) is available the vector
table is simply left empty -- retrieval degrades to BM25 + graph, never fails.
"""

from __future__ import annotations

from collections.abc import Iterable
from itertools import combinations
from pathlib import Path
from typing import Protocol

from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.source import iter_source_chunks
from arcmemory.mdfile import parse_document
from arcmemory.security import content_hash
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.stores.semantic import extract_wiki_links
from arcmemory.tagging import entity_vocabulary, tag_entities
from arcmemory.types import Scope

try:  # optional [vec] extra
    import sqlite_vec

    _SQLITE_VEC_IMPORTABLE = True
except ImportError:  # pragma: no cover
    _SQLITE_VEC_IMPORTABLE = False


class EmbeddingUnavailableError(Exception):
    """A *wired* embedder that cannot serve this call — arcmemory degrades.

    Distinct from ``embedder is None`` (never wired): the arcllm-backed adapter
    raises this when the underlying backend is genuinely absent (e.g. the local
    model extra is not installed) so retrieval/consolidation fall back to
    BM25 + graph instead of crashing (REQ-041). Never surfaced to the agent.
    """


class Embedder(Protocol):
    """Vector seam: turn texts into fixed-width vectors. Injected, not imported.

    Async so an implementation can ``await arcllm.embed`` directly on the event
    loop that already drives ``retrieve``/``consolidate`` — no nested loop, no
    ``run_until_complete``, no blocking bridge (the loop-safe seam, Phase 10).
    """

    async def embed_texts(self, texts: list[str]) -> list[list[float]]: ...


async def embed_or_none(embedder: Embedder | None, texts: list[str]) -> list[list[float]] | None:
    """Embed through the seam, or ``None`` when embeddings are unavailable.

    The single degrade funnel every call site shares: ``None`` embedder (never
    wired) and a wired-but-unavailable embedder (``EmbeddingUnavailableError``) both
    collapse to ``None`` so the caller drops the vector channel and never raises.
    """
    if embedder is None:
        return None
    if not texts:
        return []
    try:
        return await embedder.embed_texts(texts)
    except EmbeddingUnavailableError:
        return None


class IndexRebuilder:
    """Re-derives fts_chunks + vec0 + edges from files + the raw stream."""

    def __init__(
        self,
        db: MemoryDB,
        workspace: Path,
        scope: Scope,
        *,
        config: MemoryConfig | None = None,
        embedder: Embedder | None = None,
        seed_vocabulary: Iterable[str] | None = None,
    ) -> None:
        self._db = db
        self._workspace = Path(workspace)
        self._scope = scope
        self._cfg = config or MemoryConfig()
        self._embedder = embedder
        self._graph = WeightedGraph(db, self._cfg)
        self._episodic = EpisodicStore(db, workspace)
        self._mem_dir = self._workspace / "memory"
        self._seed_vocab = set(seed_vocabulary or [])

    async def rebuild(self) -> None:
        """Wipe every derived table and re-derive it from truth (idempotent).

        The single canonical wipe: ``chunks`` is cleared so rows for deleted sources
        do not survive as orphans, and ``insight_trigger`` is cleared so a poisoned or
        orphaned abstraction-space vector cannot outlive the rebuild that is meant to
        fix it (the next ``trigger_index`` re-embeds the current insight set).
        """
        conn = self._db.connect()
        conn.execute("DELETE FROM fts_chunks")
        conn.execute("DELETE FROM edges")
        conn.execute("DELETE FROM chunks")
        conn.execute("DELETE FROM insight_trigger")
        if self._db.vec_available:
            conn.execute("DELETE FROM vec0")
        conn.commit()

        await self._rebuild_chunks()
        self._rebuild_link_edges()
        self._rebuild_assoc_edges()

    # -- chunks + fts + vectors -------------------------------------------

    async def _rebuild_chunks(self) -> None:
        """Chunk every source file + every raw event; index into fts + vec.

        ``mtime`` is written ``None`` (not the file/event time the shared iterator
        carries) so a rebuild is byte-identical regardless of on-disk stats.
        """
        conn = self._db.connect()
        events = self._episodic.events(self._scope.key)
        chunks = list(iter_source_chunks(self._mem_dir, self._workspace, events))

        embeddings = await self._embed([sc.text for sc in chunks])
        for i, sc in enumerate(chunks):
            conn.execute(
                "INSERT OR REPLACE INTO chunks "
                "(chunk_id, scope, source_path, mtime, classification, content_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (sc.chunk_id, self._scope.key, sc.source_path, None, sc.classification,
                 content_hash(sc.text)),
            )
            conn.execute(
                "INSERT INTO fts_chunks (chunk_id, scope, text) VALUES (?, ?, ?)",
                (sc.chunk_id, self._scope.key, sc.text),
            )
            if embeddings is not None:
                conn.execute(
                    "INSERT INTO vec0 (chunk_id, embedding) VALUES (?, ?)",
                    (sc.chunk_id, sqlite_vec.serialize_float32(embeddings[i])),
                )
        conn.commit()

    async def _embed(self, texts: list[str]) -> list[list[float]] | None:
        """Embed chunk texts through the injected seam, or None when unavailable."""
        if not self._db.vec_available or not _SQLITE_VEC_IMPORTABLE:
            return None
        return await embed_or_none(self._embedder, texts)

    # -- edges -------------------------------------------------------------

    def _rebuild_link_edges(self) -> None:
        """Re-derive wiki-link edges from entity files (deterministic ts)."""
        entities_dir = self._mem_dir / "entities"
        if not entities_dir.exists():
            return
        for path in sorted(entities_dir.glob("*.md")):
            fm, body = parse_document(path.read_text(encoding="utf-8"))
            ts = f"{fm.get('last_updated', '1970-01-01')}T00:00:00+00:00"
            targets: list[str] = []
            for ref in fm.get("links_to", []):
                targets.extend(extract_wiki_links(str(ref)) or [str(ref)])
            targets.extend(extract_wiki_links(body))
            for target in sorted(set(targets)):
                self._graph.link(self._scope.key, path.stem, target, kind="link", ts=ts)

    def _rebuild_assoc_edges(self) -> None:
        """Replay the raw stream to reproduce Hebbian co-activation edges."""
        vocab = entity_vocabulary(self._mem_dir, self._seed_vocab)
        for event in self._episodic.events(self._scope.key):
            tags = tag_entities(event.text, vocab)
            for a, b in combinations(tags, 2):
                self._graph.hebbian_bump(self._scope.key, a, b, ts=event.ts)


__all__ = ["Embedder", "EmbeddingUnavailableError", "IndexRebuilder", "embed_or_none"]
