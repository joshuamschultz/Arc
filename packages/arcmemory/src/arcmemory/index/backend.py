"""IndexBackend — the pluggable low-level chunk/fts/vec store (SPEC-073 COMP-007).

``SurfaceIndex`` (index/surface.py) owns RRF fusion, graph spreading-activation,
recency ordering, and the degrade decision; everything below that line — the raw
``chunks``/``fts_chunks``/``vec0`` SQL — lives here instead, behind an async,
``@runtime_checkable`` Protocol. ``open_index_backend`` is the factory (mirrors
``arcstore.backends.open_backend``): callers select a backend *by name* via
``MemoryConfig.index_backend``, so swapping storage is a config change, not a
code edit. ``postgres`` is declared but deferred — a clear ``NotImplementedError``
beats a silent sqlite fallback under a Postgres config.

CRITICAL FIX carried by this module: ``vec_search`` is scope-isolated via a join
against ``chunks`` (``vec0`` itself carries no scope column). The prior inline
implementation in ``surface.py`` scanned ``vec0`` globally, leaking one agent's
chunk ids into another agent's vector search (LLM08).
"""

from __future__ import annotations

import sqlite3
import struct
from typing import Protocol, runtime_checkable

from arcmemory.db import MemoryDB

try:  # optional [vec] extra — guarded, mirrors db.py
    import sqlite_vec

    _SQLITE_VEC_IMPORTABLE = True
except ImportError:  # pragma: no cover - exercised only where the extra is absent
    _SQLITE_VEC_IMPORTABLE = False

#: Backend names declared but not yet implemented — raise, never silently fall back.
_DEFERRED = frozenset({"postgres"})


@runtime_checkable
class IndexBackend(Protocol):
    """Low-level chunk/fts/vec store the ``SurfaceIndex`` calls.

    Async and scope-isolated. RRF fusion, graph spreading-activation, recency-as-
    ranked-list, and the degrade gate live ABOVE this Protocol, in ``surface.py``,
    unchanged.
    """

    @property
    def vec_available(self) -> bool:
        """Whether the vector channel (``vec0``) is usable on this backend."""
        ...

    async def upsert_chunk(
        self,
        *,
        scope: str,
        chunk_id: str,
        source_path: str,
        mtime: float | None,
        classification: str,
        content_hash: str,
        text: str,
        embedding: list[float] | None,
    ) -> None:
        """Write/refresh one chunk's provenance row, FTS text, and (if given) vector."""
        ...

    async def stored_hashes(self, scope: str) -> dict[str, str]:
        """``chunk_id -> content_hash`` for every chunk indexed under ``scope``."""
        ...

    async def delete_scope(self, scope: str) -> None:
        """Remove every chunk/fts/vec row for ``scope`` (rebuild's wipe step)."""
        ...

    async def vec_search(self, scope: str, query_embedding: list[float]) -> list[str]:
        """Scope-filtered cosine search; best match first. ``[]`` when unavailable."""
        ...

    async def bm25_search(self, scope: str, query: str) -> list[str]:
        """FTS5/BM25 chunk ids for ``query`` within ``scope`` (best match first)."""
        ...

    async def chunk_texts(self, scope: str) -> list[tuple[str, str]]:
        """``(chunk_id, text)`` for every chunk in ``scope`` (graph-scan input)."""
        ...

    async def recency_order(self, scope: str) -> list[str]:
        """Every chunk id in ``scope``, newest first."""
        ...

    async def chunk_meta(
        self, scope: str, chunk_id: str
    ) -> tuple[str, str, float | None] | None:
        """``(source_path, classification, mtime)`` for one chunk, or ``None``."""
        ...

    async def chunk_text(self, scope: str, chunk_id: str) -> str | None:
        """The stored FTS text for one chunk, or ``None`` if it doesn't exist."""
        ...


class SqliteIndexBackend:
    """The default ``IndexBackend`` — the existing per-agent ``MemoryDB`` SQLite."""

    def __init__(self, db: MemoryDB) -> None:
        self._db = db

    @property
    def vec_available(self) -> bool:
        return self._db.vec_available

    async def upsert_chunk(
        self,
        *,
        scope: str,
        chunk_id: str,
        source_path: str,
        mtime: float | None,
        classification: str,
        content_hash: str,
        text: str,
        embedding: list[float] | None,
    ) -> None:
        conn = self._db.connect()
        conn.execute(
            "INSERT OR REPLACE INTO chunks "
            "(chunk_id, scope, source_path, mtime, classification, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chunk_id, scope, source_path, mtime, classification, content_hash),
        )
        conn.execute(
            "DELETE FROM fts_chunks WHERE chunk_id=? AND scope=?",
            (chunk_id, scope),
        )
        conn.execute(
            "INSERT INTO fts_chunks (chunk_id, scope, text) VALUES (?, ?, ?)",
            (chunk_id, scope, text),
        )
        if embedding is not None and self.vec_available:
            conn.execute("DELETE FROM vec0 WHERE chunk_id=?", (chunk_id,))
            conn.execute(
                "INSERT INTO vec0 (chunk_id, embedding) VALUES (?, ?)",
                (chunk_id, sqlite_vec.serialize_float32(embedding)),
            )
        conn.commit()

    async def stored_hashes(self, scope: str) -> dict[str, str]:
        conn = self._db.connect()
        return {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT chunk_id, content_hash FROM chunks WHERE scope=?", (scope,)
            ).fetchall()
        }

    async def delete_scope(self, scope: str) -> None:
        conn = self._db.connect()
        if self.vec_available:
            ids = [
                row[0]
                for row in conn.execute(
                    "SELECT chunk_id FROM chunks WHERE scope=?", (scope,)
                ).fetchall()
            ]
            if ids:
                # Placeholders are only "?" repeated per bound id — never interpolated
                # data — and every value is still passed as a SQL parameter below.
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"DELETE FROM vec0 WHERE chunk_id IN ({placeholders})",  # noqa: S608
                    ids,
                )
        conn.execute("DELETE FROM fts_chunks WHERE scope=?", (scope,))
        conn.execute("DELETE FROM chunks WHERE scope=?", (scope,))
        conn.commit()

    async def vec_search(self, scope: str, query_embedding: list[float]) -> list[str]:
        """Brute-force cosine over ``vec0``, JOINed to ``chunks`` for scope isolation.

        ``vec0`` carries no scope column of its own; the join is what stops a search
        scoped to one agent from ever scoring another agent's vectors (LLM08).
        """
        if not self.vec_available:
            return []
        conn = self._db.connect()
        rows = conn.execute(
            "SELECT v.chunk_id, v.embedding FROM vec0 v "
            "JOIN chunks c ON c.chunk_id = v.chunk_id WHERE c.scope = ?",
            (scope,),
        ).fetchall()
        scored: list[tuple[float, str]] = []
        for chunk_id, blob in rows:
            vector = list(struct.unpack(f"{len(blob) // 4}f", blob))
            scored.append((_cosine(query_embedding, vector), chunk_id))
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [chunk_id for score, chunk_id in scored if score > 0.0]

    async def bm25_search(self, scope: str, query: str) -> list[str]:
        if not query:
            return []
        conn = self._db.connect()
        rows = conn.execute(
            "SELECT chunk_id FROM fts_chunks "
            "WHERE scope=? AND fts_chunks MATCH ? ORDER BY bm25(fts_chunks)",
            (scope, query),
        ).fetchall()
        return [row[0] for row in rows]

    async def chunk_texts(self, scope: str) -> list[tuple[str, str]]:
        conn = self._db.connect()
        rows = conn.execute(
            "SELECT chunk_id, text FROM fts_chunks WHERE scope=?", (scope,)
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    async def recency_order(self, scope: str) -> list[str]:
        conn = self._db.connect()
        rows = conn.execute(
            "SELECT chunk_id FROM chunks WHERE scope=? ORDER BY COALESCE(mtime, 0) DESC, chunk_id",
            (scope,),
        ).fetchall()
        return [row[0] for row in rows]

    async def chunk_meta(
        self, scope: str, chunk_id: str
    ) -> tuple[str, str, float | None] | None:
        conn = self._db.connect()
        row = conn.execute(
            "SELECT source_path, classification, mtime FROM chunks WHERE chunk_id=? AND scope=?",
            (chunk_id, scope),
        ).fetchone()
        if row is None:
            return None
        return (row[0], row[1], row[2])

    async def chunk_text(self, scope: str, chunk_id: str) -> str | None:
        conn = self._db.connect()
        row: sqlite3.Row | None = conn.execute(
            "SELECT text FROM fts_chunks WHERE chunk_id=? AND scope=?",
            (chunk_id, scope),
        ).fetchone()
        return row[0] if row is not None else None


def open_index_backend(backend: str = "sqlite", *, db: MemoryDB) -> IndexBackend:
    """Return an ``IndexBackend`` for the named backend.

    ``sqlite`` is the only concrete backend today; ``postgres`` raises
    ``NotImplementedError`` (deferred behind the Protocol). An unknown name is a
    ``ValueError`` at the config boundary.
    """
    if backend == "sqlite":
        return SqliteIndexBackend(db)
    if backend in _DEFERRED:
        raise NotImplementedError(
            f"arcmemory index backend {backend!r} is deferred behind the IndexBackend "
            "Protocol (SPEC-073 COMP-007); only 'sqlite' is implemented."
        )
    raise ValueError(f"Unknown arcmemory index backend: {backend!r}. Use 'sqlite'.")


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors (0.0 on a zero vector)."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(dot / (norm_a * norm_b))


__all__ = ["IndexBackend", "SqliteIndexBackend", "open_index_backend"]
