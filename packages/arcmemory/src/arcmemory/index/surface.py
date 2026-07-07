"""Surface retrieval — the *easy* channel: vec cosine + BM25 + graph, RRF-fused.

Surface recall answers "what past text looks like this query" three ways and fuses
them (SDD 4.5, R-2/R-3/R-11):

* **vec** — cosine over the ``vec0`` embedding table (semantic; catches paraphrase
  with no shared tokens). Brute-force in Python: per-agent chunk counts are in the
  low tens-of-thousands, where a full scan is sub-20ms and needs no ANN (R-11).
* **bm25** — FTS5 keyword match (lexical; exact-term precision).
* **graph** — spreading activation from the query's tagged entities to the chunks
  that mention them (associative; reinforced pairs light up).

The three ranked lists are fused with Reciprocal Rank Fusion (``1/(k+rank)``, k=60),
and **recency is a fourth ranked list** rather than a score multiplier — that keeps
RRF's scale-free property while still letting newer/reinforced items win a tie
(R-11 corrects the naive "recency-weight" multiplier).

Indexing is **incremental and content-gated** (T-040, LLM10): only new or changed
chunks are embedded, so re-indexing a mostly-unchanged workspace is nearly free.

Degrade (T-042, REQ-041): when ``sqlite-vec`` did not load or no embedder is
injected, the vec list is simply dropped — BM25 + graph still answer, a
``recall.degraded`` audit event is emitted, and nothing raises.
"""

from __future__ import annotations

import sqlite3
import struct
from collections.abc import Iterable
from pathlib import Path

from arctrust.audit import AuditEvent, AuditSink, NullSink, emit
from pydantic import BaseModel, Field

from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.rebuild import Embedder, embed_or_none
from arcmemory.mdfile import parse_document
from arcmemory.security import content_hash
from arcmemory.stores.episodic import EpisodicStore
from arcmemory.tagging import tag_entities
from arcmemory.types import Recall, Scope

try:  # optional [vec] extra — guarded, mirrors db.py
    import sqlite_vec

    _SQLITE_VEC_IMPORTABLE = True
except ImportError:  # pragma: no cover - exercised only where the extra is absent
    _SQLITE_VEC_IMPORTABLE = False

_RRF_K = 60
# Curated markdown source directories, fixed order (determinism), matching rebuild.
_SOURCE_SUBDIRS = ("entities", "insights", "procedures", "daily-log")


class SurfaceResult(BaseModel):
    """The bounded surface-channel result: ranked recalls + a degrade flag."""

    recalls: list[Recall] = Field(default_factory=list)
    degraded: bool = False


class _Chunk(BaseModel):
    """One indexable unit — a source file or a raw event, with its gate hash."""

    chunk_id: str
    source_path: str
    text: str
    classification: str
    mtime: float
    content_hash: str


class SurfaceIndex:
    """Incremental surface index + fused search for one agent scope."""

    def __init__(
        self,
        db: MemoryDB,
        workspace: Path,
        scope: Scope,
        *,
        config: MemoryConfig | None = None,
        embedder: Embedder | None = None,
        audit_sink: AuditSink | None = None,
        seed_vocabulary: Iterable[str] | None = None,
    ) -> None:
        self._db = db
        self._workspace = Path(workspace)
        self._scope = scope
        self._cfg = config or MemoryConfig()
        self._embedder = embedder
        self._audit = audit_sink if audit_sink is not None else NullSink()
        self._graph = WeightedGraph(db, self._cfg)
        self._episodic = EpisodicStore(db, workspace)
        self._mem_dir = self._workspace / "memory"
        self._seed_vocab = set(seed_vocabulary or [])

    # -- indexing ----------------------------------------------------------

    async def index_if_needed(self) -> int:
        """Embed + index only new/changed chunks; return how many were (re)indexed."""
        conn = self._db.connect()
        stored = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT chunk_id, content_hash FROM chunks WHERE scope=?", (self._scope.key,)
            ).fetchall()
        }
        changed = [c for c in self._collect_chunks() if stored.get(c.chunk_id) != c.content_hash]
        if not changed:
            return 0

        embeddings = await self._embed([c.text for c in changed])
        for i, chunk in enumerate(changed):
            self._upsert_chunk(conn, chunk, embeddings[i] if embeddings is not None else None)
        conn.commit()
        return len(changed)

    def _collect_chunks(self) -> list[_Chunk]:
        """Every source file + every raw event, as gate-hashed chunks (fixed order)."""
        chunks: list[_Chunk] = []
        for subdir in _SOURCE_SUBDIRS:
            directory = self._mem_dir / subdir
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.md")):
                text = path.read_text(encoding="utf-8")
                fm, _ = parse_document(text)
                rel = str(path.relative_to(self._workspace))
                chunks.append(
                    _Chunk(
                        chunk_id=f"file:{rel}",
                        source_path=rel,
                        text=text,
                        # A genuinely missing label passes through empty — the
                        # no-read-up gate decides fail-closed (federal) vs default
                        # (personal), never the index (SDD §8).
                        classification=str(fm.get("classification") or ""),
                        mtime=path.stat().st_mtime,
                        content_hash=content_hash(text),
                    )
                )
        for event in self._episodic.events(self._scope.key):
            chunks.append(
                _Chunk(
                    chunk_id=f"event:{event.event_id}",
                    source_path="episodic",
                    text=event.text,
                    classification="unclassified",
                    mtime=_iso_epoch(event.ts),
                    content_hash=content_hash(event.text),
                )
            )
        return chunks

    def _upsert_chunk(
        self, conn: sqlite3.Connection, chunk: _Chunk, embedding: list[float] | None
    ) -> None:
        """Write the provenance row, refresh FTS, and (when available) the vector."""
        conn.execute(
            "INSERT OR REPLACE INTO chunks "
            "(chunk_id, scope, source_path, mtime, classification, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                chunk.chunk_id,
                self._scope.key,
                chunk.source_path,
                chunk.mtime,
                chunk.classification,
                chunk.content_hash,
            ),
        )
        conn.execute(
            "DELETE FROM fts_chunks WHERE chunk_id=? AND scope=?",
            (chunk.chunk_id, self._scope.key),
        )
        conn.execute(
            "INSERT INTO fts_chunks (chunk_id, scope, text) VALUES (?, ?, ?)",
            (chunk.chunk_id, self._scope.key, chunk.text),
        )
        if embedding is not None:
            conn.execute("DELETE FROM vec0 WHERE chunk_id=?", (chunk.chunk_id,))
            conn.execute(
                "INSERT INTO vec0 (chunk_id, embedding) VALUES (?, ?)",
                (chunk.chunk_id, sqlite_vec.serialize_float32(embedding)),
            )

    async def _embed(self, texts: list[str]) -> list[list[float]] | None:
        """Embed through the injected seam, or None when embeddings are unavailable."""
        if not self._db.vec_available or not _SQLITE_VEC_IMPORTABLE:
            return None
        return await embed_or_none(self._embedder, texts)

    # -- search ------------------------------------------------------------

    async def search(self, text: str, *, top_k: int = 5) -> SurfaceResult:
        """Fuse vec + bm25 + graph + recency; return the top-k boundary-ready recalls."""
        vec_ranked = await self._vec_search(text)
        degraded = vec_ranked is None
        ranked_lists = [self._bm25_search(text), self._graph_search(text), self._recency_order()]
        if vec_ranked is not None:
            ranked_lists.append(vec_ranked)

        fused = self._rrf(ranked_lists)
        hydrated = (self._to_recall(cid, score) for cid, score in fused[:top_k])
        recalls = [r for r in hydrated if r is not None]
        if degraded:
            self._emit_degraded(text)
        return SurfaceResult(recalls=recalls, degraded=degraded)

    def bm25_only(self, text: str, *, top_k: int = 5) -> list[str]:
        """BM25 result *content* only — a baseline for 'fusion beats BM25-alone'."""
        recalls = [self._to_recall(cid, 0.0) for cid in self._bm25_search(text)[:top_k]]
        return [r.content for r in recalls if r is not None]

    async def _vec_search(self, text: str) -> list[str] | None:
        """Brute-force cosine over ``vec0``; None when embeddings are unavailable."""
        if not self._db.vec_available or not _SQLITE_VEC_IMPORTABLE:
            return None
        vectors = await embed_or_none(self._embedder, [text])
        if not vectors:
            return None
        query = vectors[0]
        conn = self._db.connect()
        rows = conn.execute("SELECT chunk_id, embedding FROM vec0").fetchall()
        scored: list[tuple[float, str]] = []
        for chunk_id, blob in rows:
            vector = list(struct.unpack(f"{len(blob) // 4}f", blob))
            scored.append((_cosine(query, vector), chunk_id))
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [chunk_id for score, chunk_id in scored if score > 0.0]

    def _bm25_search(self, text: str) -> list[str]:
        """FTS5/BM25 chunk ids for ``text`` (best match first)."""
        query = _fts_query(text)
        if not query:
            return []
        conn = self._db.connect()
        rows = conn.execute(
            "SELECT chunk_id FROM fts_chunks "
            "WHERE scope=? AND fts_chunks MATCH ? ORDER BY bm25(fts_chunks)",
            (self._scope.key, query),
        ).fetchall()
        return [r[0] for r in rows]

    def _graph_search(self, text: str) -> list[str]:
        """Chunks whose text mentions an entity the query activates (assoc signal)."""
        vocab = self._vocabulary()
        seeds = tag_entities(text, vocab)
        if not seeds:
            return []
        activation = self._graph.spreading_activation(self._scope.key, dict.fromkeys(seeds, 1.0))
        if not activation:
            return []
        conn = self._db.connect()
        scored: list[tuple[float, str]] = []
        for chunk_id, chunk_text in conn.execute(
            "SELECT chunk_id, text FROM fts_chunks WHERE scope=?", (self._scope.key,)
        ).fetchall():
            hit = sum(act for node, act in activation.items() if node in chunk_text.lower())
            if hit > 0.0:
                scored.append((hit, chunk_id))
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [chunk_id for _, chunk_id in scored]

    def _recency_order(self) -> list[str]:
        """All chunk ids, newest first — the recency ranked list (R-11)."""
        conn = self._db.connect()
        rows = conn.execute(
            "SELECT chunk_id FROM chunks WHERE scope=? ORDER BY COALESCE(mtime, 0) DESC, chunk_id",
            (self._scope.key,),
        ).fetchall()
        return [r[0] for r in rows]

    def _rrf(self, ranked_lists: list[list[str]]) -> list[tuple[str, float]]:
        """Reciprocal-rank-fuse ranked lists into one descending (id, score) list."""
        scores: dict[str, float] = {}
        for ranked in ranked_lists:
            for rank, chunk_id in enumerate(ranked):
                scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (_RRF_K + rank)
        return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))

    def _to_recall(self, chunk_id: str, score: float) -> Recall | None:
        """Hydrate a fused chunk id into a ``Recall`` (None if it vanished)."""
        conn = self._db.connect()
        meta = conn.execute(
            "SELECT source_path, classification FROM chunks WHERE chunk_id=? AND scope=?",
            (chunk_id, self._scope.key),
        ).fetchone()
        text_row = conn.execute(
            "SELECT text FROM fts_chunks WHERE chunk_id=? AND scope=?",
            (chunk_id, self._scope.key),
        ).fetchone()
        if meta is None or text_row is None:
            return None
        return Recall(
            source=chunk_id,
            content=text_row[0],
            score=score,
            kind="surface",
            classification=str(meta[1]),
        )

    def _vocabulary(self) -> set[str]:
        """Tagging vocabulary: seed terms + slugs of existing entity files."""
        vocab = set(self._seed_vocab)
        entities_dir = self._mem_dir / "entities"
        if entities_dir.exists():
            vocab.update(p.stem for p in entities_dir.glob("*.md"))
        return vocab

    def _emit_degraded(self, text: str) -> None:
        """Signal (never raise) that retrieval ran without the vector channel."""
        emit(
            AuditEvent(
                actor_did=self._scope.agent_did,
                action="recall.degraded",
                target="surface.search",
                outcome="allow",
                tier=self._cfg.tier,
                payload_hash=content_hash(text),
                extra={"reason": "embeddings_unavailable"},
            ),
            self._audit,
        )


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors (0.0 on a zero vector)."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(dot / (norm_a * norm_b))


def _fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 OR-query of its alphanumeric tokens."""
    tokens = [t for t in _tokenize(text) if t]
    return " OR ".join(f'"{t}"' for t in tokens)


def _tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens (FTS-safe; drops punctuation/operators)."""
    return ["".join(ch for ch in word if ch.isalnum()).lower() for word in text.split()]


def _iso_epoch(ts: str) -> float:
    """Best-effort epoch seconds from an ISO timestamp (0.0 when unparseable)."""
    from datetime import datetime

    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return 0.0


__all__ = ["SurfaceIndex", "SurfaceResult"]
