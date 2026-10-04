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

Scope isolation (LLM08): ``vec0`` carries no scope column, so the SQLite backend
answers vector search from a per-scope HNSW sidecar (``index/ann.py``) and maps its
keys back through ``chunks`` filtered on ``scope``. The Postgres backend keeps the
same isolation with a ``WHERE scope = $1`` on its single ``chunks`` table.

Bounded and off-loop: every ranked channel takes a top-k (``VEC_SEARCH_TOP_K``
by default) and every SQLite call runs on the DB's worker thread
(``MemoryDB.run``). A 978k-vector pool scored in Python on the event loop once
blocked it past the 60 s systemd watchdog.

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
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from arcmemory.config import MemoryConfig
from arcmemory.db import PENDING_EMBED_SQL, MemoryDB
from arcmemory.index import ann

try:  # optional [vec] extra — guarded, mirrors db.py
    import sqlite_vec

    _SQLITE_VEC_IMPORTABLE = True
except ImportError:  # pragma: no cover - exercised only where the extra is absent
    _SQLITE_VEC_IMPORTABLE = False


#: Default depth of every ranked channel (vec, bm25, recency): each returns at
#: most this many ids, best first, so fusion above is O(k), not O(scope).
VEC_SEARCH_TOP_K = 200
#: Ids bound per ``IN (...)`` statement, well under SQLite's variable limit.
_SQL_BATCH = 500
#: Most postings (summed document counts of the kept terms) one BM25 query may
#: score: ~50 ms of FTS5 work on a 2026 laptop core.
_BM25_POSTINGS_BUDGET = 100_000

# Embed-backfill queries. ``PENDING_EMBED_SQL`` is a module constant (no caller
# data is ever interpolated), and must appear verbatim for SQLite to use the
# ``idx_chunks_embed_pending`` partial index.
_SQLITE_PENDING_COUNTS = (
    "SELECT scope, COUNT(*) FROM chunks INDEXED BY idx_chunks_embed_pending "  # noqa: S608
    f"WHERE substr(scope, 1, ?) = ? AND {PENDING_EMBED_SQL} GROUP BY scope"
)
# Page the partial index first, then fetch each row's text by its rowid: a plain
# join would let the planner start from the FTS table instead.
_SQLITE_PENDING_PAGE = (
    "SELECT pending.chunk_id, pending.content_hash, fts_chunks.text FROM ("  # noqa: S608
    "SELECT chunk_id, content_hash, fts_rowid FROM chunks "
    f"WHERE scope=? AND chunk_id>? AND {PENDING_EMBED_SQL} "
    "ORDER BY chunk_id LIMIT ?) AS pending "
    "JOIN fts_chunks ON fts_chunks.rowid = pending.fts_rowid "
    "ORDER BY pending.chunk_id"
)
_PG_EMBED_BACKLOG = (
    "SELECT scope, COUNT(*) AS total, "  # noqa: S608
    f"COUNT(*) FILTER (WHERE {PENDING_EMBED_SQL}) AS pending "
    "FROM chunks WHERE left(scope, $1) = $2 GROUP BY scope"
)
_PG_PENDING_PAGE = (
    "SELECT chunk_id, content_hash, text FROM chunks "  # noqa: S608
    f"WHERE scope=$1 AND chunk_id > $2 AND {PENDING_EMBED_SQL} "
    "ORDER BY chunk_id LIMIT $3"
)


@dataclass(frozen=True)
class ChunkWrite:
    """One chunk to write through :meth:`IndexBackend.upsert_chunks`."""

    chunk_id: str
    source_path: str
    mtime: float | None
    classification: str
    content_hash: str
    text: str
    embedding: list[float] | None


@dataclass(frozen=True)
class PendingEmbed:
    """One chunk whose vector is missing or stale, as read for the embed backfill."""

    chunk_id: str
    content_hash: str
    text: str


@dataclass(frozen=True)
class EmbeddingWrite:
    """A vector for one EXISTING chunk, valid only for the content it was embedded from."""

    chunk_id: str
    content_hash: str
    embedding: list[float]


@dataclass(frozen=True)
class EmbedBacklog:
    """One scope's vector coverage: every chunk, and those still awaiting a vector."""

    total: int
    pending: int

    @property
    def embedded(self) -> int:
        return self.total - self.pending


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

    async def upsert_chunks(self, scope: str, chunks: Sequence[ChunkWrite]) -> None:
        """Write every chunk of one object as one transaction (all or none).

        One commit per object, not per chunk: a commit per chunk cost one WAL
        sync and one FTS segment flush each, on the event loop.
        """
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

    async def embed_backlog(self, scope_prefix: str) -> dict[str, EmbedBacklog]:
        """Vector coverage of every scope whose key starts with ``scope_prefix``.

        Every such scope is listed, fully embedded ones too. A chunk is pending
        when it has no vector or its vector was embedded for older content
        (``embedded_hash`` absent or != ``content_hash``).
        """
        ...

    async def pending_embeds(self, scope: str, *, after: str, limit: int) -> list[PendingEmbed]:
        """At most ``limit`` pending chunks of ``scope`` with ``chunk_id > after``, in id order.

        A keyset page: callers stream a million-chunk backlog without holding it.
        """
        ...

    async def set_embeddings(self, scope: str, writes: Sequence[EmbeddingWrite]) -> int:
        """Store vectors for existing chunks of ``scope``; return how many were written.

        Writes ONLY the vector and ``embedded_hash``: text, FTS rows and provenance
        are untouched. A write whose ``content_hash`` no longer matches the chunk
        (its content changed while it was being embedded), or whose chunk is gone
        or belongs to another scope, is skipped and the chunk stays pending.
        """
        ...

    async def delete_scope(self, scope: str) -> None:
        """Remove every chunk/fts/vec row for ``scope`` (rebuild's wipe step)."""
        ...

    async def delete_object(self, scope: str, object_id: str) -> None:
        """Remove exactly one object's chunk membership from ``scope``."""
        ...

    async def repath_object(self, scope: str, object_id: str, source_path: str) -> int:
        """Point one object's chunks at a new ``source_path``; return rows changed.

        Metadata only: chunk ids, text and vectors are untouched, so a document
        that moves on disk keeps its embeddings. Idempotent: a chunk already at
        ``source_path`` is not counted.
        """
        ...

    async def vec_search(
        self, scope: str, query_embedding: list[float], top_k: int = VEC_SEARCH_TOP_K
    ) -> list[str]:
        """Scope-filtered cosine top-``top_k``; best match first. ``[]`` when unavailable."""
        ...

    async def bm25_search(
        self, scope: str, query: str, top_k: int = VEC_SEARCH_TOP_K
    ) -> list[str]:
        """Top-``top_k`` FTS/BM25 chunk ids for ``query`` within ``scope`` (best first)."""
        ...

    async def chunk_texts(
        self, scope: str, *, terms: Sequence[str] | None = None, limit: int | None = None
    ) -> list[tuple[str, str]]:
        """``(chunk_id, text)`` in ``scope``, at most ``limit`` rows (``None``: all).

        With ``terms``, only chunks whose text matches any term (as a phrase),
        newest first — the bounded candidate set of the graph channel.
        """
        ...

    async def recency_order(self, scope: str, limit: int | None = VEC_SEARCH_TOP_K) -> list[str]:
        """The ``limit`` newest chunk ids in ``scope`` (``None``: every one), newest first."""
        ...

    async def chunk_meta(self, scope: str, chunk_id: str) -> tuple[str, str, float | None] | None:
        """``(source_path, classification, mtime)`` for one chunk, or ``None``."""
        ...

    async def chunk_text(self, scope: str, chunk_id: str) -> str | None:
        """The stored FTS text for one chunk, or ``None`` if it doesn't exist."""
        ...

    async def scope_classifications(self, scope: str) -> set[str]:
        """Distinct labels of ``scope``'s content chunks, collection indexes excluded."""
        ...

    async def scopes_with_prefix(self, prefix: str) -> list[str]:
        """Every distinct scope key that starts with ``prefix``, sorted."""
        ...


class SqliteIndexBackend:
    """The default ``IndexBackend`` — the existing per-agent ``MemoryDB`` SQLite.

    Every method runs its SQL on the DB's worker thread (``MemoryDB.run``), never
    on the event loop. Vector search goes through the per-scope HNSW sidecar
    (``index/ann.py``); every ``vec0`` write here stamps the scope's generation
    and hands the exact change to that sidecar after commit.
    """

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
        await self.upsert_chunks(
            scope,
            [
                ChunkWrite(
                    chunk_id=chunk_id,
                    source_path=source_path,
                    mtime=mtime,
                    classification=classification,
                    content_hash=content_hash,
                    text=text,
                    embedding=embedding,
                )
            ],
        )

    async def upsert_chunks(self, scope: str, chunks: Sequence[ChunkWrite]) -> None:
        vec = self.vec_available

        def work(conn: sqlite3.Connection) -> None:
            changes = _VectorChanges()
            try:
                for chunk in chunks:
                    _write_chunk(conn, scope, chunk, changes if vec else None)
                deltas = changes.stamp(conn)
            except BaseException:
                conn.rollback()
                raise
            conn.commit()
            ann.apply_deltas(self._db, deltas)

        await self._db.run(work)

    async def stored_hashes(self, scope: str) -> dict[str, str]:
        return await self._db.run(
            lambda conn: {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT chunk_id, content_hash FROM chunks WHERE scope=?", (scope,)
                )
            }
        )

    async def stored_mtimes(self, scope: str) -> dict[str, float]:
        return await self._db.run(
            lambda conn: {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT chunk_id, mtime FROM chunks WHERE scope=? AND mtime IS NOT NULL",
                    (scope,),
                )
            }
        )

    async def embedded_hashes(self, scope: str) -> dict[str, str]:
        return await self._db.run(
            lambda conn: {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT chunk_id, embedded_hash FROM chunks "
                    "WHERE scope=? AND embedded_hash IS NOT NULL",
                    (scope,),
                )
            }
        )

    async def embed_backlog(self, scope_prefix: str) -> dict[str, EmbedBacklog]:
        def work(conn: sqlite3.Connection) -> dict[str, EmbedBacklog]:
            params = (len(scope_prefix), scope_prefix)
            pending = dict(
                conn.execute(
                    _SQLITE_PENDING_COUNTS,
                    params,
                ).fetchall()
            )
            totals = conn.execute(
                "SELECT scope, COUNT(*) FROM chunks WHERE substr(scope, 1, ?) = ? GROUP BY scope",
                params,
            ).fetchall()
            return {
                str(scope): EmbedBacklog(total=int(total), pending=int(pending.get(scope, 0)))
                for scope, total in totals
            }

        return await self._db.run(work)

    async def pending_embeds(self, scope: str, *, after: str, limit: int) -> list[PendingEmbed]:
        if limit <= 0:
            return []
        return await self._db.run(
            lambda conn: [
                PendingEmbed(chunk_id=str(row[0]), content_hash=str(row[1]), text=str(row[2]))
                for row in conn.execute(
                    _SQLITE_PENDING_PAGE,
                    (scope, after, limit),
                )
            ]
        )

    async def set_embeddings(self, scope: str, writes: Sequence[EmbeddingWrite]) -> int:
        if not self.vec_available or not writes:
            return 0

        def work(conn: sqlite3.Connection) -> int:
            changes = _VectorChanges()
            try:
                written = sum(_write_embedding(conn, scope, write, changes) for write in writes)
                deltas = changes.stamp(conn)
            except BaseException:
                conn.rollback()
                raise
            conn.commit()
            ann.apply_deltas(self._db, deltas)
            return written

        return await self._db.run(work)

    async def delete_scope(self, scope: str) -> None:
        vec = self.vec_available

        def work(conn: sqlite3.Connection) -> None:
            deltas: dict[str, ann.VectorDelta] = {}
            try:
                if vec:
                    ids = [
                        row[0]
                        for row in conn.execute(
                            "SELECT chunk_id FROM chunks WHERE scope=?", (scope,)
                        ).fetchall()
                    ]
                    _delete_vectors(conn, ids)
                    after = ann.mark_scope_written(conn, scope)
                    deltas[scope] = ann.VectorDelta(after - 1, after, reset=True)
                conn.execute("DELETE FROM fts_chunks WHERE scope=?", (scope,))
                conn.execute("DELETE FROM chunks WHERE scope=?", (scope,))
            except BaseException:
                conn.rollback()
                raise
            conn.commit()
            ann.apply_deltas(self._db, deltas)

        await self._db.run(work)

    async def delete_object(self, scope: str, object_id: str) -> None:
        vec = self.vec_available

        def work(conn: sqlite3.Connection) -> None:
            rows = _object_rows(conn, scope, object_id)
            if not rows:
                return
            ids = [row[0] for row in rows]
            deltas: dict[str, ann.VectorDelta] = {}
            try:
                if vec:
                    _delete_vectors(conn, ids)
                    after = ann.mark_scope_written(conn, scope)
                    deltas[scope] = ann.VectorDelta(
                        after - 1, after, removed=[row[2] for row in rows]
                    )
                text_rows = [row[1] for row in rows if row[1] is not None]
                if text_rows:
                    marks = ",".join("?" for _ in text_rows)
                    conn.execute(
                        f"DELETE FROM fts_chunks WHERE rowid IN ({marks})",  # noqa: S608
                        text_rows,
                    )
                placeholders = ",".join("?" for _ in ids)
                conn.execute(f"DELETE FROM chunks WHERE chunk_id IN ({placeholders})", ids)  # noqa: S608
            except BaseException:
                conn.rollback()
                raise
            conn.commit()
            ann.apply_deltas(self._db, deltas)

        await self._db.run(work)

    async def repath_object(self, scope: str, object_id: str, source_path: str) -> int:
        def work(conn: sqlite3.Connection) -> int:
            # The same primary-key range as ``delete_object``: exactly ``<object_id>#…``.
            cursor = conn.execute(
                "UPDATE chunks SET source_path=? WHERE scope=? AND chunk_id>=? AND chunk_id<? "
                "AND source_path<>?",
                (source_path, scope, object_id + "#", object_id + "$", source_path),
            )
            conn.commit()
            return cursor.rowcount

        return await self._db.run(work)

    async def vec_search(
        self, scope: str, query_embedding: list[float], top_k: int = VEC_SEARCH_TOP_K
    ) -> list[str]:
        """Top-k cosine via the scope's HNSW sidecar; nearest first, never another scope.

        The index is per scope and the key -> chunk map re-checks ``scope``, so
        a search scoped to one agent can never return another's chunk (LLM08).
        Chunks with a non-positive similarity are not returned.
        """
        if not self.vec_available or top_k <= 0:
            return []
        index = ann.scope_ann(self._db, scope)
        generation = await self._db.run(lambda conn: ann.read_generation(conn, scope))
        hits = await asyncio.to_thread(index.search, query_embedding, top_k, generation)
        if not hits:
            return []
        return await self._db.run(lambda conn: ann.keys_to_chunk_ids(conn, scope, hits))

    async def bm25_search(
        self, scope: str, query: str, top_k: int = VEC_SEARCH_TOP_K
    ) -> list[str]:
        if not query or top_k <= 0:
            return []

        def work(conn: sqlite3.Connection) -> list[str]:
            return [
                row[0]
                for row in conn.execute(
                    "SELECT chunk_id FROM fts_chunks "
                    "WHERE scope=? AND fts_chunks MATCH ? ORDER BY bm25(fts_chunks) LIMIT ?",
                    (scope, _affordable_fts_query(conn, query), top_k),
                )
            ]

        return await self._db.run(work)

    async def chunk_texts(
        self, scope: str, *, terms: Sequence[str] | None = None, limit: int | None = None
    ) -> list[tuple[str, str]]:
        bound = -1 if limit is None else limit
        if terms is None:
            sql = "SELECT chunk_id, text FROM fts_chunks WHERE scope=? LIMIT ?"
            params: tuple[Any, ...] = (scope, bound)
        else:
            query = _phrase_query(terms)
            if not query:
                return []
            # Newest first by rowid, NOT by bm25: FTS5 walks doclists in rowid
            # order, so the LIMIT stops the scan early instead of scoring every
            # chunk that names a common entity.
            sql = (
                "SELECT chunk_id, text FROM fts_chunks WHERE scope=? AND fts_chunks MATCH ? "
                "ORDER BY rowid DESC LIMIT ?"
            )
            params = (scope, query, bound)
        return await self._db.run(
            lambda conn: [(row[0], row[1]) for row in conn.execute(sql, params)]
        )

    async def recency_order(self, scope: str, limit: int | None = VEC_SEARCH_TOP_K) -> list[str]:
        return await self._db.run(
            lambda conn: [
                row[0]
                for row in conn.execute(
                    "SELECT chunk_id FROM chunks WHERE scope=? "
                    "ORDER BY COALESCE(mtime, 0) DESC, chunk_id LIMIT ?",
                    (scope, -1 if limit is None else limit),
                )
            ]
        )

    async def chunk_meta(self, scope: str, chunk_id: str) -> tuple[str, str, float | None] | None:
        row = await self._db.run(
            lambda conn: conn.execute(
                "SELECT source_path, classification, mtime FROM chunks "
                "WHERE chunk_id=? AND scope=?",
                (chunk_id, scope),
            ).fetchone()
        )
        if row is None:
            return None
        return (row[0], row[1], row[2])

    async def chunk_text(self, scope: str, chunk_id: str) -> str | None:
        row = await self._db.run(
            lambda conn: conn.execute(
                "SELECT fts_chunks.text FROM chunks "
                "JOIN fts_chunks ON fts_chunks.rowid = chunks.fts_rowid "
                "WHERE chunks.chunk_id=? AND chunks.scope=?",
                (chunk_id, scope),
            ).fetchone()
        )
        return str(row[0]) if row is not None else None

    async def scope_classifications(self, scope: str) -> set[str]:
        """Distinct labels of a scope's content chunks (collection indexes excluded)."""
        return await self._db.run(
            lambda conn: {
                str(row[0] or "")
                for row in conn.execute(
                    "SELECT DISTINCT classification FROM chunks "
                    "WHERE scope=? AND substr(chunk_id, 1, 6) != 'index:'",
                    (scope,),
                )
            }
        )

    async def scopes_with_prefix(self, prefix: str) -> list[str]:
        return await self._db.run(
            lambda conn: [
                str(row[0])
                for row in conn.execute(
                    "SELECT DISTINCT scope FROM chunks WHERE substr(scope, 1, ?) = ? "
                    "ORDER BY scope",
                    (len(prefix), prefix),
                )
            ]
        )


class _VectorChanges:
    """Vector adds/removes gathered across one write transaction, per scope."""

    def __init__(self) -> None:
        self._added: dict[str, dict[int, bytes]] = {}
        self._removed: dict[str, list[int]] = {}

    def add(self, scope: str, key: int, blob: bytes) -> None:
        self._added.setdefault(scope, {})[key] = blob

    def remove(self, scope: str, key: int) -> None:
        self._removed.setdefault(scope, []).append(key)

    def stamp(self, conn: sqlite3.Connection) -> dict[str, ann.VectorDelta]:
        """Bump each touched scope's generation (inside the open txn); return its delta."""
        deltas: dict[str, ann.VectorDelta] = {}
        for scope in sorted({*self._added, *self._removed}):
            after = ann.mark_scope_written(conn, scope)
            deltas[scope] = ann.VectorDelta(
                after - 1,
                after,
                removed=self._removed.get(scope, []),
                added=self._added.get(scope, {}),
            )
        return deltas


def _object_rows(
    conn: sqlite3.Connection, scope: str, object_id: str
) -> list[tuple[str, int | None, int]]:
    """``(chunk_id, fts_rowid, key)`` of one object's chunks in ``scope``.

    A primary-key range, not ``LIKE``: LIKE cannot use the key index, so every
    object delete walked the scope's rows (and, on ``fts_chunks``, the whole text
    index). ``#`` + 1 is ``$``, so the range is exactly the ``<object_id>#…``
    windows; a collection index also owns its bare id.
    """
    prefix = object_id + "#"
    return [
        (chunk_id, fts_rowid, int(key))
        for chunk_id, fts_rowid, owner, key in conn.execute(
            "SELECT chunk_id, fts_rowid, scope, rowid FROM chunks "
            "WHERE chunk_id=? OR (chunk_id>=? AND chunk_id<?)",
            (object_id if object_id.startswith("index:") else prefix, prefix, object_id + "$"),
        ).fetchall()
        if owner == scope
    ]


def _write_chunk(
    conn: sqlite3.Connection, scope: str, chunk: ChunkWrite, changes: _VectorChanges | None
) -> None:
    """Write one chunk's provenance row, text row and (if given) vector.

    ``changes`` is ``None`` when sqlite-vec is unavailable (no ``vec0``).
    """
    # ``ON CONFLICT ... DO UPDATE`` (not ``INSERT OR REPLACE``): REPLACE deletes
    # and re-inserts the row, so any column omitted from the statement — here
    # ``embedded_hash`` — would silently reset to NULL on every lexical-only
    # write, erasing the record that this chunk's vector is already current
    # (H-REG-1). The UPDATE form touches exactly the columns named, so a
    # ``None`` embedding preserves whatever ``embedded_hash`` was already
    # stored, and a real embedding stamps it to this write's content_hash. It
    # also keeps the row id, which is the chunk's key in the HNSW sidecar.
    embedded_hash = chunk.content_hash if chunk.embedding is not None else None
    prior = conn.execute(
        "SELECT fts_rowid, scope, rowid FROM chunks WHERE chunk_id=?", (chunk.chunk_id,)
    ).fetchone()
    # The text row is replaced through the rowid its chunk row remembers —
    # a lookup by ``fts_chunks.chunk_id`` (UNINDEXED) reads the whole index.
    if prior is not None and prior[1] == scope and prior[0] is not None:
        conn.execute("DELETE FROM fts_chunks WHERE rowid=?", (prior[0],))
    fts_rowid = conn.execute(
        "INSERT INTO fts_chunks (chunk_id, scope, text) VALUES (?, ?, ?)",
        (chunk.chunk_id, scope, chunk.text),
    ).lastrowid
    conn.execute(
        "INSERT INTO chunks "
        "(chunk_id, scope, source_path, mtime, classification, content_hash, embedded_hash, "
        "fts_rowid) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(chunk_id) DO UPDATE SET "
        "scope=excluded.scope, source_path=excluded.source_path, mtime=excluded.mtime, "
        "classification=excluded.classification, content_hash=excluded.content_hash, "
        "embedded_hash=COALESCE(excluded.embedded_hash, chunks.embedded_hash), "
        "fts_rowid=excluded.fts_rowid",
        (
            chunk.chunk_id,
            scope,
            chunk.source_path,
            chunk.mtime,
            chunk.classification,
            chunk.content_hash,
            embedded_hash,
            fts_rowid,
        ),
    )
    if changes is not None:
        _write_vector(conn, scope, chunk, prior, changes)


def _write_vector(
    conn: sqlite3.Connection,
    scope: str,
    chunk: ChunkWrite,
    prior: tuple[Any, ...] | None,
    changes: _VectorChanges,
) -> None:
    """Write the chunk's vector and record what the sidecar must change.

    A chunk id is global, so an upsert under a new scope MOVES the chunk (and any
    vector it already has): the old scope's index must drop it (LLM08) and the
    new scope's must gain it, even on a lexical-only write.
    """
    key = int(prior[2]) if prior is not None else _chunk_key(conn, chunk.chunk_id)
    moved_from = prior[1] if prior is not None and prior[1] != scope else None
    if moved_from is not None:
        changes.remove(moved_from, key)
    if chunk.embedding is not None:
        blob = sqlite_vec.serialize_float32(chunk.embedding)
        conn.execute("DELETE FROM vec0 WHERE chunk_id=?", (chunk.chunk_id,))
        conn.execute(
            "INSERT INTO vec0 (chunk_id, embedding) VALUES (?, ?)", (chunk.chunk_id, blob)
        )
        changes.add(scope, key, blob)
    elif moved_from is not None:
        row = conn.execute(
            "SELECT embedding FROM vec0 WHERE chunk_id=?", (chunk.chunk_id,)
        ).fetchone()
        if row is not None:
            changes.add(scope, key, bytes(row[0]))


def _write_embedding(
    conn: sqlite3.Connection, scope: str, write: EmbeddingWrite, changes: _VectorChanges
) -> bool:
    """Store one chunk's vector iff the chunk is still in ``scope`` with this content."""
    row = conn.execute(
        "SELECT rowid FROM chunks WHERE chunk_id=? AND scope=? AND content_hash=?",
        (write.chunk_id, scope, write.content_hash),
    ).fetchone()
    if row is None:
        return False
    blob = sqlite_vec.serialize_float32(write.embedding)
    conn.execute("DELETE FROM vec0 WHERE chunk_id=?", (write.chunk_id,))
    conn.execute("INSERT INTO vec0 (chunk_id, embedding) VALUES (?, ?)", (write.chunk_id, blob))
    conn.execute("UPDATE chunks SET embedded_hash=? WHERE rowid=?", (write.content_hash, row[0]))
    changes.add(scope, int(row[0]), blob)
    return True


def _chunk_key(conn: sqlite3.Connection, chunk_id: str) -> int:
    row = conn.execute("SELECT rowid FROM chunks WHERE chunk_id=?", (chunk_id,)).fetchone()
    return int(row[0])


def _delete_vectors(conn: sqlite3.Connection, chunk_ids: Sequence[str]) -> None:
    """Delete these chunks' ``vec0`` rows (``vec0`` carries no scope column)."""
    for offset in range(0, len(chunk_ids), _SQL_BATCH):
        batch = list(chunk_ids[offset : offset + _SQL_BATCH])
        # Placeholders are only "?" repeated per bound id — never interpolated data.
        marks = ",".join("?" for _ in batch)
        conn.execute(f"DELETE FROM vec0 WHERE chunk_id IN ({marks})", batch)  # noqa: S608


def _affordable_fts_query(conn: sqlite3.Connection, query: str) -> str:
    """Narrow an OR-of-terms query to the terms that fit the postings budget.

    BM25 scores every chunk any OR-ed term matches, so one near-universal word in
    a long question made a 1.2M-chunk search cost seconds. Terms are taken
    rarest first while their combined document count fits
    ``_BM25_POSTINGS_BUDGET``; the rarest is always kept. A term this common
    carries almost no IDF weight, so the ranking barely moves. A term the
    vocabulary does not list costs nothing and is always kept. Any query that is
    not a plain OR of quoted terms is returned unchanged.
    """
    terms = list(dict.fromkeys(re.findall(r'"([^"]+)"', query)))
    if len(terms) < 2 or query != " OR ".join(f'"{term}"' for term in terms):
        return query
    doc_count = {term: _term_doc_count(conn, term) for term in terms}
    kept: list[str] = []
    spent = 0
    for term in sorted(terms, key=lambda term: (doc_count[term], term)):
        if kept and spent + doc_count[term] > _BM25_POSTINGS_BUDGET:
            break
        kept.append(term)
        spent += doc_count[term]
    return " OR ".join(f'"{term}"' for term in kept)


def _term_doc_count(conn: sqlite3.Connection, term: str) -> int:
    row = conn.execute("SELECT doc FROM fts_chunks_vocab WHERE term=?", (term,)).fetchone()
    return int(row[0]) if row else 0


def _phrase_query(terms: Sequence[str]) -> str:
    """An FTS5 OR-of-phrases over ``terms``' alnum tokens (operator-free, LLM01)."""
    phrases = []
    for term in terms:
        tokens = [token for token in re.split(r"[^0-9A-Za-z]+", term.lower()) if token]
        if tokens:
            phrases.append('"' + " ".join(tokens) + '"')
    return " OR ".join(dict.fromkeys(phrases))


# Module-level pool cache keyed by DSN, guarded by a lock: per-op backend
# construction (open_index_backend runs on every DocIndex/SurfaceIndex build)
# reuses one asyncpg pool + one schema-init per DSN. asyncpg publishes no stubs,
# so the pool is typed ``Any`` at this boundary (mirrors the [postgres] mypy
# override); every value crossing the seam is coerced to a concrete type below.
_POOLS: dict[str, Any] = {}
_POOL_LOCK = asyncio.Lock()


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
        await self.upsert_chunks(
            scope,
            [
                ChunkWrite(
                    chunk_id=chunk_id,
                    source_path=source_path,
                    mtime=mtime,
                    classification=classification,
                    content_hash=content_hash,
                    text=text,
                    embedding=embedding,
                )
            ],
        )

    async def upsert_chunks(self, scope: str, chunks: Sequence[ChunkWrite]) -> None:
        pool = await self._pool()
        # H-REG-1: a lexical-only write (``embedding is None``) must neither wipe an
        # existing vector nor claim (via ``embedded_hash``) that a vector was written
        # for content it never embedded — ``COALESCE(EXCLUDED.x, chunks.x)`` on both
        # columns preserves the prior value whenever this write carries no embedding,
        # exactly mirroring the sqlite backend's ``ON CONFLICT`` behavior.
        rows = [
            (
                scope,
                chunk.chunk_id,
                chunk.text,
                chunk.embedding,
                chunk.source_path,
                chunk.mtime,
                chunk.classification,
                chunk.content_hash,
                chunk.content_hash if chunk.embedding is not None else None,
            )
            for chunk in chunks
        ]
        async with pool.acquire() as conn, conn.transaction():
            await conn.executemany(
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
                rows,
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

    async def embed_backlog(self, scope_prefix: str) -> dict[str, EmbedBacklog]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                _PG_EMBED_BACKLOG,
                len(scope_prefix),
                scope_prefix,
            )
        return {
            str(row["scope"]): EmbedBacklog(total=int(row["total"]), pending=int(row["pending"]))
            for row in rows
        }

    async def pending_embeds(self, scope: str, *, after: str, limit: int) -> list[PendingEmbed]:
        if limit <= 0:
            return []
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                _PG_PENDING_PAGE,
                scope,
                after,
                limit,
            )
        return [
            PendingEmbed(
                chunk_id=str(row["chunk_id"]),
                content_hash=str(row["content_hash"]),
                text=str(row["text"]),
            )
            for row in rows
        ]

    async def set_embeddings(self, scope: str, writes: Sequence[EmbeddingWrite]) -> int:
        if not writes:
            return 0
        pool = await self._pool()
        written = 0
        async with pool.acquire() as conn, conn.transaction():
            for write in writes:
                status: str = await conn.execute(
                    "UPDATE chunks SET embedding=$3, embedded_hash=$4 "
                    "WHERE scope=$1 AND chunk_id=$2 AND content_hash=$4",
                    scope,
                    write.chunk_id,
                    write.embedding,
                    write.content_hash,
                )
                written += int(status.rsplit(" ", 1)[-1])
        return written

    async def delete_scope(self, scope: str) -> None:
        pool = await self._pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM chunks WHERE scope=$1", scope)

    async def delete_object(self, scope: str, object_id: str) -> None:
        pool = await self._pool()
        # Same rows as the SQLite backend: the ``<object_id>#…`` windows, and for
        # a collection index its bare id too. ``starts_with`` (not LIKE) cannot
        # read ``%``/``_`` inside an object id as wildcards, and is independent of
        # the database collation.
        prefix = object_id + "#"
        exact = object_id if object_id.startswith("index:") else prefix
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM chunks WHERE scope=$1 AND (chunk_id=$2 OR starts_with(chunk_id, $3))",
                scope,
                exact,
                prefix,
            )

    async def repath_object(self, scope: str, object_id: str, source_path: str) -> int:
        pool = await self._pool()
        async with pool.acquire() as conn:
            status: str = await conn.execute(
                "UPDATE chunks SET source_path=$3 "
                "WHERE scope=$1 AND starts_with(chunk_id, $2) AND source_path<>$3",
                scope,
                object_id + "#",
                source_path,
            )
        return int(status.rsplit(" ", 1)[-1])

    async def vec_search(
        self, scope: str, query_embedding: list[float], top_k: int = VEC_SEARCH_TOP_K
    ) -> list[str]:
        """Scope-filtered cosine ANN via the pgvector ``<=>`` operator, nearest first."""
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT chunk_id FROM chunks "
                "WHERE scope=$1 AND embedding IS NOT NULL "
                "ORDER BY embedding <=> $2 LIMIT $3",
                scope,
                query_embedding,
                top_k,
            )
        return [str(row["chunk_id"]) for row in rows]

    async def bm25_search(
        self, scope: str, query: str, top_k: int = VEC_SEARCH_TOP_K
    ) -> list[str]:
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
                "ORDER BY ts_rank_cd(tsv, to_tsquery('english', $2)) DESC LIMIT $3",
                scope,
                tsquery,
                top_k,
            )
        return [str(row["chunk_id"]) for row in rows]

    async def chunk_texts(
        self, scope: str, *, terms: Sequence[str] | None = None, limit: int | None = None
    ) -> list[tuple[str, str]]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            if terms is None:
                rows = await conn.fetch(
                    "SELECT chunk_id, text FROM chunks WHERE scope=$1 LIMIT $2", scope, limit
                )
            else:
                tsquery = _phrase_tsquery(terms)
                if not tsquery:
                    return []
                rows = await conn.fetch(
                    "SELECT chunk_id, text FROM chunks "
                    "WHERE scope=$1 AND tsv @@ to_tsquery('english', $2) "
                    "ORDER BY ts_rank_cd(tsv, to_tsquery('english', $2)) DESC LIMIT $3",
                    scope,
                    tsquery,
                    limit,
                )
        return [(str(row["chunk_id"]), str(row["text"])) for row in rows]

    async def recency_order(self, scope: str, limit: int | None = VEC_SEARCH_TOP_K) -> list[str]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT chunk_id FROM chunks WHERE scope=$1 "
                "ORDER BY COALESCE(mtime, 0) DESC, chunk_id LIMIT $2",
                scope,
                limit,
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

    async def scope_classifications(self, scope: str) -> set[str]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT classification FROM chunks "
                "WHERE scope=$1 AND substr(chunk_id, 1, 6) <> 'index:'",
                scope,
            )
        return {str(row["classification"] or "") for row in rows}

    async def scopes_with_prefix(self, prefix: str) -> list[str]:
        pool = await self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT scope FROM chunks WHERE left(scope, $1) = $2 ORDER BY scope",
                len(prefix),
                prefix,
            )
        return [str(row["scope"]) for row in rows]


def _parse_fts_tokens(query: str) -> list[str]:
    """Extract the alnum tokens from an FTS5-formatted query (``'"a" OR "b"'``).

    Only the quoted lexemes are kept, and each is stripped to alnum, so the ``OR``
    operator never leaks into ``to_tsquery`` (a stopword between ``|`` operators is
    a tsquery syntax error) and no token can inject tsquery operators (LLM01).
    """
    tokens = [re.sub(r"[^A-Za-z0-9]", "", raw) for raw in re.findall(r'"([^"]+)"', query)]
    return [token for token in tokens if token]


def _phrase_tsquery(terms: Sequence[str]) -> str:
    """A tsquery OR-of-phrases over ``terms``' alnum tokens (operator-free, LLM01)."""
    phrases = []
    for term in terms:
        tokens = [token for token in re.split(r"[^0-9A-Za-z]+", term.lower()) if token]
        if tokens:
            phrases.append("(" + " <-> ".join(tokens) + ")")
    return " | ".join(dict.fromkeys(phrases))


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


#: The scope-key segment every document pool carries (``<did>:doc:<source>``,
#: see :func:`arcmemory.doc_index.doc_scope`).
DOC_SCOPE_MARKER = ":doc:"


def is_doc_pool_scope(scope: str) -> bool:
    """Whether ``scope`` names a document pool rather than an agent's memory scope."""
    return DOC_SCOPE_MARKER in scope


def doc_pool_backend(db: MemoryDB) -> IndexBackend:
    """THE store an agent's document pools live in: its workspace ``index.db``.

    The one answer to "where do doc pools live", used by every doc path —
    connected-data writes, the Brain's ``ingest_batch`` document home,
    ``document_search``, operator views and the embed backfill. It does not
    follow ``MemoryConfig.index_backend``: that setting places the agent's own
    memory scope, and connected-data ports never carried it, so honouring it
    here split one agent's pools across two stores (the Brain searched an empty
    Postgres while every document sat in SQLite). A shared connection store is
    the same rule applied to that store's own workspace.
    """
    return SqliteIndexBackend(db)


def backend_for_scope(scope: str, config: MemoryConfig, db: MemoryDB) -> IndexBackend:
    """The backend holding ``scope``: the doc-pool store for a document pool, else the
    configured memory backend (``config.index_backend``)."""
    if is_doc_pool_scope(scope):
        return doc_pool_backend(db)
    return open_index_backend(config.index_backend, db=db)


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors (0.0 on a zero vector)."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(dot / (norm_a * norm_b))


__all__ = [
    "DOC_SCOPE_MARKER",
    "VEC_SEARCH_TOP_K",
    "ChunkWrite",
    "EmbedBacklog",
    "EmbeddingWrite",
    "IndexBackend",
    "PendingEmbed",
    "PostgresIndexBackend",
    "SqliteIndexBackend",
    "backend_for_scope",
    "doc_pool_backend",
    "is_doc_pool_scope",
    "open_index_backend",
]
