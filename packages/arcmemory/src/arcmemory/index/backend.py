"""IndexBackend — the pluggable low-level chunk/fts/vec store (SPEC-073 COMP-007).

``SurfaceIndex`` (index/surface.py) owns RRF fusion, graph spreading-activation,
recency ordering, and the degrade decision; everything below that line — the raw
``chunks``/``fts_chunks``/``vec0`` SQL — lives here instead, behind an async,
``@runtime_checkable`` Protocol. ``open_index_backend`` is the factory (mirrors
``arcstore.backends.open_backend``): callers select a backend *by name* via
``MemoryConfig.index_backend``, so swapping storage is a config change, not a
code edit. ``sqlite`` (default, per-agent file) and ``postgres`` (pgvector, a
shared server) are the two concrete backends; an unknown name is a plain
configuration ``ValueError``.

CRITICAL FIX carried by this module: ``vec_search`` is scope-isolated via a join
against ``chunks`` (``vec0`` itself carries no scope column). The prior inline
implementation in ``surface.py`` scanned ``vec0`` globally, leaking one agent's
chunk ids into another agent's vector search (LLM08). The Postgres backend keeps
the same isolation with a ``WHERE scope = $1`` on its single ``chunks`` table.

``asyncpg``/``pgvector`` are the optional ``[postgres]`` extra, lazy-imported
inside the Postgres methods so importing this module never needs them; absence at
factory time raises a clear ``RuntimeError`` naming the extra, never an
``ImportError``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import sqlite3
import struct
from typing import Any, Protocol, runtime_checkable

from arcmemory.db import MemoryDB

try:  # optional [vec] extra — guarded, mirrors db.py
    import sqlite_vec

    _SQLITE_VEC_IMPORTABLE = True
except ImportError:  # pragma: no cover - exercised only where the extra is absent
    _SQLITE_VEC_IMPORTABLE = False


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

    async def stored_mtimes(self, scope: str) -> dict[str, float]:
        """``chunk_id -> mtime`` for every chunk indexed under ``scope``.

        The turn-path bound (H-REG-1): lets a caller decide a FILE chunk is
        provably unchanged from a cheap ``stat()`` alone, without opening and
        hashing its body, so ``index_if_needed`` need not re-read the whole
        corpus on every turn — only files whose on-disk mtime actually moved.
        """
        ...

    async def embedded_hashes(self, scope: str) -> dict[str, str]:
        """``chunk_id -> embedded_hash`` for every chunk that HAS a vector.

        Distinct from :meth:`stored_hashes`: a chunk written lexically-only
        (``embedding=None``) gets a ``content_hash`` but no ``embedded_hash`` entry
        here, so a caller can tell "fts is fresh" apart from "the vector is fresh"
        (H-REG-1's hash-gate trap) — the two channels are gated on separate hashes.
        """
        ...

    async def delete_scope(self, scope: str) -> None:
        """Remove every chunk/fts/vec row for ``scope`` (rebuild's wipe step)."""
        ...

    async def delete_object(self, scope: str, object_id: str) -> None:
        """Remove exactly one object's chunk membership from ``scope``."""
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

    async def chunk_meta(self, scope: str, chunk_id: str) -> tuple[str, str, float | None] | None:
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
        # ``ON CONFLICT ... DO UPDATE`` (not ``INSERT OR REPLACE``): REPLACE deletes
        # and re-inserts the row, so any column omitted from the statement — here
        # ``embedded_hash`` — would silently reset to NULL on every lexical-only
        # write, erasing the record that this chunk's vector is already current
        # (H-REG-1). The UPDATE form touches exactly the columns named, so a
        # ``None`` embedding preserves whatever ``embedded_hash`` was already
        # stored, and a real embedding stamps it to this write's content_hash.
        embedded_hash = content_hash if embedding is not None else None
        conn.execute(
            "INSERT INTO chunks "
            "(chunk_id, scope, source_path, mtime, classification, content_hash, embedded_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(chunk_id) DO UPDATE SET "
            "scope=excluded.scope, source_path=excluded.source_path, mtime=excluded.mtime, "
            "classification=excluded.classification, content_hash=excluded.content_hash, "
            "embedded_hash=COALESCE(excluded.embedded_hash, chunks.embedded_hash)",
            (chunk_id, scope, source_path, mtime, classification, content_hash, embedded_hash),
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

    async def stored_mtimes(self, scope: str) -> dict[str, float]:
        conn = self._db.connect()
        return {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT chunk_id, mtime FROM chunks WHERE scope=? AND mtime IS NOT NULL",
                (scope,),
            ).fetchall()
        }

    async def embedded_hashes(self, scope: str) -> dict[str, str]:
        conn = self._db.connect()
        return {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT chunk_id, embedded_hash FROM chunks "
                "WHERE scope=? AND embedded_hash IS NOT NULL",
                (scope,),
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

    async def delete_object(self, scope: str, object_id: str) -> None:
        conn = self._db.connect()
        pattern = object_id if object_id.startswith("index:") else object_id + "#%"
        ids = [
            row[0]
            for row in conn.execute(
                "SELECT chunk_id FROM chunks WHERE scope=? AND chunk_id LIKE ?",
                (scope, pattern),
            ).fetchall()
        ]
        if self.vec_available and ids:
            placeholders = ",".join("?" for _ in ids)
            conn.execute(f"DELETE FROM vec0 WHERE chunk_id IN ({placeholders})", ids)  # noqa: S608
        conn.execute("DELETE FROM fts_chunks WHERE scope=? AND chunk_id LIKE ?", (scope, pattern))
        conn.execute("DELETE FROM chunks WHERE scope=? AND chunk_id LIKE ?", (scope, pattern))
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

    async def chunk_meta(self, scope: str, chunk_id: str) -> tuple[str, str, float | None] | None:
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


# Module-level pool cache keyed by DSN, guarded by a lock: per-op backend
# construction (open_index_backend runs on every DocIndex/SurfaceIndex build)
# reuses one asyncpg pool + one schema-init per DSN. asyncpg publishes no stubs,
# so the pool is typed ``Any`` at this boundary (mirrors the [postgres] mypy
# override); every value crossing the seam is coerced to a concrete type below.
_POOLS: dict[str, Any] = {}
_POOL_LOCK = asyncio.Lock()

#: Bound on rows returned by the ANN scan — nearest-first, then RRF-fused above.
_VEC_SEARCH_LIMIT = 200


class PostgresIndexBackend:
    """``IndexBackend`` over Postgres + pgvector — a shared server for many agents.

    Unlike sqlite's chunks/fts_chunks/vec0 split, everything lives in ONE
    ``chunks`` table: ``(scope, chunk_id)`` PK, ``text``, ``embedding
    vector(dims)``, ``tsv tsvector``, plus provenance. Every query is filtered by
    ``scope`` (the agent DID), so one agent never sees another's rows. The pool is
    created lazily on first use and cached module-wide keyed by DSN.
    """

    def __init__(self, dsn: str, dims: int) -> None:
        self._dsn = dsn
        self._dims = dims

    @property
    def vec_available(self) -> bool:
        return True  # pgvector always provides the vector channel

    async def _pool(self) -> Any:
        """Return the cached asyncpg pool for this DSN, creating it once.

        asyncpg + pgvector are imported here, not at module load, so importing
        ``backend.py`` never requires the ``[postgres]`` extra. The pool's ``init``
        registers the pgvector codec on every connection; schema DDL runs once,
        under the lock, right after the pool is created.
        """
        import asyncpg  # lazy: only the postgres path needs the extra
        from pgvector.asyncpg import register_vector

        async with _POOL_LOCK:
            pool = _POOLS.get(self._dsn)
            if pool is None:

                async def _init(conn: Any) -> None:
                    await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
                    await register_vector(conn)

                pool = await asyncpg.create_pool(self._dsn, init=_init)
                await self._ensure_schema(pool)
                _POOLS[self._dsn] = pool
            return pool

    async def _ensure_schema(self, pool: Any) -> None:
        # ``dims`` is an int (MemoryDB.dims), never user text — safe to inline.
        create_table = (
            "CREATE TABLE IF NOT EXISTS chunks ("
            "scope TEXT NOT NULL, "
            "chunk_id TEXT NOT NULL, "
            "text TEXT NOT NULL, "
            f"embedding vector({self._dims}), "  # dims is a trusted int, never user text
            "tsv tsvector, "
            "source_path TEXT NOT NULL, "
            "mtime DOUBLE PRECISION, "
            "classification TEXT NOT NULL, "
            "content_hash TEXT NOT NULL, "
            "PRIMARY KEY (scope, chunk_id))"
        )
        async with pool.acquire() as conn:
            await conn.execute(create_table)
            # H-REG-1: self-migration for a table created before this column existed —
            # mirrors ``MemoryDB._ensure_columns`` (sqlite's equivalent seam). Checked
            # BEFORE the ALTER (unlike sqlite's rowcount-free ``ADD COLUMN IF NOT
            # EXISTS``) so the migration backfill below can be gated on "did this
            # call actually create the column" rather than running unconditionally.
            had_embedded_hash = bool(
                await conn.fetchval(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name='chunks' AND column_name='embedded_hash'"
                )
            )
            await conn.execute("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embedded_hash TEXT")
            if not had_embedded_hash:
                # Migration backfill (production hazard): a server upgraded from
                # before this column existed already has real vectors for rows the
                # column starts NULL on. Left unfixed, the first post-deploy embed
                # pass would see every one of those rows as "never embedded" and
                # re-embed the ENTIRE existing corpus — months of memory, a
                # cost/latency spike of the same class as the past consolidation
                # runaway. Stamping ``embedded_hash = content_hash`` for every row
                # that already carries a vector marks exactly those rows resolved.
                # Gated on ``had_embedded_hash`` (false only the one time the
                # column is actually created), so this is a no-op — safe to call —
                # on an already-migrated database.
                await conn.execute(
                    "UPDATE chunks SET embedded_hash = content_hash WHERE embedding IS NOT NULL"
                )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw "
                "ON chunks USING hnsw (embedding vector_cosine_ops)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS chunks_tsv_gin ON chunks USING gin (tsv)"
            )
            await conn.execute("CREATE INDEX IF NOT EXISTS chunks_scope_btree ON chunks (scope)")

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
        pool = await self._pool()
        # H-REG-1: a lexical-only write (``embedding is None``) must neither wipe an
        # existing vector nor claim (via ``embedded_hash``) that a vector was written
        # for content it never embedded — ``COALESCE(EXCLUDED.x, chunks.x)`` on both
        # columns preserves the prior value whenever this write carries no embedding,
        # exactly mirroring the sqlite backend's ``ON CONFLICT`` behavior.
        embedded_hash = content_hash if embedding is not None else None
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO chunks "
                "(scope, chunk_id, text, embedding, tsv, source_path, mtime, "
                "classification, content_hash, embedded_hash) "
                "VALUES ($1, $2, $3, $4, to_tsvector('english', $3), $5, $6, $7, $8, $9) "
                "ON CONFLICT (scope, chunk_id) DO UPDATE SET "
                "text = EXCLUDED.text, "
                "embedding = COALESCE(EXCLUDED.embedding, chunks.embedding), "
                "tsv = EXCLUDED.tsv, source_path = EXCLUDED.source_path, "
                "mtime = EXCLUDED.mtime, classification = EXCLUDED.classification, "
                "content_hash = EXCLUDED.content_hash, "
                "embedded_hash = COALESCE(EXCLUDED.embedded_hash, chunks.embedded_hash)",
                scope,
                chunk_id,
                text,
                embedding,
                source_path,
                mtime,
                classification,
                content_hash,
                embedded_hash,
            )

    async def stored_hashes(self, scope: str) -> dict[str, str]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT chunk_id, content_hash FROM chunks WHERE scope=$1", scope
            )
        return {str(row["chunk_id"]): str(row["content_hash"]) for row in rows}

    async def stored_mtimes(self, scope: str) -> dict[str, float]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT chunk_id, mtime FROM chunks WHERE scope=$1 AND mtime IS NOT NULL", scope
            )
        return {str(row["chunk_id"]): float(row["mtime"]) for row in rows}

    async def embedded_hashes(self, scope: str) -> dict[str, str]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT chunk_id, embedded_hash FROM chunks "
                "WHERE scope=$1 AND embedded_hash IS NOT NULL",
                scope,
            )
        return {str(row["chunk_id"]): str(row["embedded_hash"]) for row in rows}

    async def delete_scope(self, scope: str) -> None:
        pool = await self._pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM chunks WHERE scope=$1", scope)

    async def delete_object(self, scope: str, object_id: str) -> None:
        pool = await self._pool()
        pattern = object_id if object_id.startswith("index:") else object_id + "#%"
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM chunks WHERE scope=$1 AND chunk_id LIKE $2",
                scope,
                pattern,
            )

    async def vec_search(self, scope: str, query_embedding: list[float]) -> list[str]:
        """Scope-filtered cosine ANN via the pgvector ``<=>`` operator, nearest first."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT chunk_id FROM chunks "
                "WHERE scope=$1 AND embedding IS NOT NULL "
                "ORDER BY embedding <=> $2 LIMIT $3",
                scope,
                query_embedding,
                _VEC_SEARCH_LIMIT,
            )
        return [str(row["chunk_id"]) for row in rows]

    async def bm25_search(self, scope: str, query: str) -> list[str]:
        """FTS via ``to_tsquery``; parses the FTS5-formatted query into OR'd tokens."""
        tokens = _parse_fts_tokens(query)
        if not tokens:
            return []
        tsquery = " | ".join(tokens)
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT chunk_id FROM chunks "
                "WHERE scope=$1 AND tsv @@ to_tsquery('english', $2) "
                "ORDER BY ts_rank_cd(tsv, to_tsquery('english', $2)) DESC",
                scope,
                tsquery,
            )
        return [str(row["chunk_id"]) for row in rows]

    async def chunk_texts(self, scope: str) -> list[tuple[str, str]]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT chunk_id, text FROM chunks WHERE scope=$1", scope)
        return [(str(row["chunk_id"]), str(row["text"])) for row in rows]

    async def recency_order(self, scope: str) -> list[str]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT chunk_id FROM chunks WHERE scope=$1 "
                "ORDER BY COALESCE(mtime, 0) DESC, chunk_id",
                scope,
            )
        return [str(row["chunk_id"]) for row in rows]

    async def chunk_meta(self, scope: str, chunk_id: str) -> tuple[str, str, float | None] | None:
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT source_path, classification, mtime FROM chunks "
                "WHERE scope=$1 AND chunk_id=$2",
                scope,
                chunk_id,
            )
        if row is None:
            return None
        mtime = row["mtime"]
        return (
            str(row["source_path"]),
            str(row["classification"]),
            None if mtime is None else float(mtime),
        )

    async def chunk_text(self, scope: str, chunk_id: str) -> str | None:
        pool = await self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT text FROM chunks WHERE scope=$1 AND chunk_id=$2",
                scope,
                chunk_id,
            )
        return None if row is None else str(row["text"])


def _parse_fts_tokens(query: str) -> list[str]:
    """Extract the alnum tokens from an FTS5-formatted query (``'"a" OR "b"'``).

    Only the quoted lexemes are kept, and each is stripped to alnum, so the ``OR``
    operator never leaks into ``to_tsquery`` (a stopword between ``|`` operators is
    a tsquery syntax error) and no token can inject tsquery operators (LLM01).
    """
    tokens = [re.sub(r"[^A-Za-z0-9]", "", raw) for raw in re.findall(r'"([^"]+)"', query)]
    return [token for token in tokens if token]


def open_index_backend(
    backend: str = "sqlite", *, db: MemoryDB, dsn: str | None = None
) -> IndexBackend:
    """Return an ``IndexBackend`` for the named backend.

    ``sqlite`` (default) uses the per-agent ``MemoryDB``. ``postgres`` resolves its
    DSN from ``dsn`` (arg first) else ``ARC_MEMORY_PG_DSN`` — no DSN is a
    ``ValueError`` at the config boundary — and requires the ``[postgres]`` extra;
    its absence is a clear ``RuntimeError``, never a leaked ``ImportError``. An
    unknown name is a ``ValueError``.
    """
    if backend == "sqlite":
        return SqliteIndexBackend(db)
    if backend == "postgres":
        resolved = dsn if dsn is not None else os.environ.get("ARC_MEMORY_PG_DSN")
        if not resolved:
            raise ValueError("postgres index backend requires a DSN (set ARC_MEMORY_PG_DSN)")
        if importlib.util.find_spec("asyncpg") is None:
            raise RuntimeError(
                "postgres index backend requires asyncpg + pgvector; install arcmemory[postgres]"
            )
        return PostgresIndexBackend(resolved, db.dims)
    raise ValueError(f"Unknown arcmemory index backend: {backend!r}. Use 'sqlite' or 'postgres'.")


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors (0.0 on a zero vector)."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(dot / (norm_a * norm_b))


__all__ = [
    "IndexBackend",
    "PostgresIndexBackend",
    "SqliteIndexBackend",
    "open_index_backend",
]
