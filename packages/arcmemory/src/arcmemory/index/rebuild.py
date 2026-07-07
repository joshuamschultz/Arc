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
from arcmemory.mdfile import parse_document
from arcmemory.security import content_hash
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.stores.semantic import extract_wiki_links
from arcmemory.tagging import tag_entities
from arcmemory.types import Scope

try:  # optional [vec] extra
    import sqlite_vec

    _SQLITE_VEC_IMPORTABLE = True
except ImportError:  # pragma: no cover
    _SQLITE_VEC_IMPORTABLE = False

# Curated markdown source directories, in a fixed order (determinism).
_SOURCE_SUBDIRS = ("entities", "insights", "procedures", "daily-log")


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
        """Wipe all derived tables and re-derive them from truth (idempotent)."""
        conn = self._db.connect()
        conn.execute("DELETE FROM fts_chunks")
        conn.execute("DELETE FROM edges")
        conn.execute("DELETE FROM chunks")
        if self._db.vec_available:
            conn.execute("DELETE FROM vec0")
        conn.commit()

        await self._rebuild_chunks()
        self._rebuild_link_edges()
        self._rebuild_assoc_edges()

    # -- chunks + fts + vectors -------------------------------------------

    async def _rebuild_chunks(self) -> None:
        """Chunk every source file + every raw event; index into fts + vec."""
        conn = self._db.connect()
        chunks: list[tuple[str, str, str, str]] = []  # (chunk_id, source, text, classification)

        for subdir in _SOURCE_SUBDIRS:
            directory = self._mem_dir / subdir
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.md")):
                text = path.read_text(encoding="utf-8")
                fm, _ = parse_document(text)
                classification = str(fm.get("classification", "unclassified"))
                rel = str(path.relative_to(self._workspace))
                chunks.append((f"file:{rel}", rel, text, classification))

        for event in self._episodic.events(self._scope.key):
            chunks.append((f"event:{event.event_id}", "episodic", event.text, "unclassified"))

        embeddings = await self._embed([text for _, _, text, _ in chunks])
        for i, (chunk_id, source, text, classification) in enumerate(chunks):
            conn.execute(
                "INSERT OR REPLACE INTO chunks "
                "(chunk_id, scope, source_path, mtime, classification, content_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (chunk_id, self._scope.key, source, None, classification, content_hash(text)),
            )
            conn.execute(
                "INSERT INTO fts_chunks (chunk_id, scope, text) VALUES (?, ?, ?)",
                (chunk_id, self._scope.key, text),
            )
            if embeddings is not None:
                conn.execute(
                    "INSERT INTO vec0 (chunk_id, embedding) VALUES (?, ?)",
                    (chunk_id, sqlite_vec.serialize_float32(embeddings[i])),
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
        vocab = set(self._seed_vocab)
        entities_dir = self._mem_dir / "entities"
        if entities_dir.exists():
            vocab.update(p.stem for p in entities_dir.glob("*.md"))

        for event in self._episodic.events(self._scope.key):
            tags = tag_entities(event.text, vocab)
            for a, b in combinations(tags, 2):
                self._graph.hebbian_bump(self._scope.key, a, b, ts=event.ts)


__all__ = ["Embedder", "EmbeddingUnavailableError", "IndexRebuilder", "embed_or_none"]
