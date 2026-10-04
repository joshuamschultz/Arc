"""Per-agent SQLite substrate — the raw stream + all derived indices.

DC-1 (load-bearing): ``arcstore`` is a closed 5-kind operational spool with no
FTS5, no ``sqlite-vec``, and no per-scope store object — it *cannot* host the
memory index. So arcmemory owns its own SQLite at ``<workspace>/memory/index.db``:
one file per agent workspace = hard shared-nothing isolation (LLM08). Everything
in this file is **disposable** — ``index/rebuild.py`` re-derives all of it from the
glass-box markdown + raw stream.

The ``sqlite-vec`` extension is optional (the ``[vec]`` extra). Its load is guarded:
absence disables the ``vec0`` table and flips ``vec_available`` to ``False`` so
retrieval degrades to BM25 + graph rather than raising.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal, TypeVar

try:  # optional [vec] extra — guarded, never fatal
    import sqlite_vec

    _SQLITE_VEC_IMPORTABLE = True
except ImportError:  # pragma: no cover - exercised only where the extra is absent
    _SQLITE_VEC_IMPORTABLE = False

from arcmemory.degrade import warn_once

#: ``full`` fsyncs every commit. ``normal`` (WAL) skips that fsync: it never
#: corrupts, but an OS crash can lose the last commits, so it is only for a
#: store whose whole content can be rebuilt from its provider.
Durability = Literal["full", "normal"]

# Default embedding width (bge-small / MiniLM are both 384-dim).
DEFAULT_DIMS = 384

_T = TypeVar("_T")

_VEC_MISSING = (
    "SEMANTIC RECALL IS OFF: the sqlite-vec package is not installed, so the vec0 "
    "table cannot exist and memory recall runs on BM25 + graph only. "
    "Reinstall arcmemory (`uv sync --all-packages`) — sqlite-vec is a base dependency."
)
_VEC_UNLOADABLE = (
    "SEMANTIC RECALL IS OFF: this Python/SQLite build cannot load the sqlite-vec "
    "extension, so memory recall runs on BM25 + graph only. Use a Python built with "
    "SQLite extension loading enabled."
)


def _load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Attempt to load the sqlite-vec extension onto ``conn``.

    Returns True on success. Guarded on both the import and the runtime
    ``enable_load_extension`` capability (some Python builds compile it out),
    so a missing extension degrades instead of crashing.
    """
    if not _SQLITE_VEC_IMPORTABLE:
        warn_once("sqlite-vec:not-installed", _VEC_MISSING)
        return False
    if not hasattr(conn, "enable_load_extension"):
        warn_once("sqlite-vec:not-loadable", _VEC_UNLOADABLE)
        return False
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except (sqlite3.OperationalError, AttributeError):
        warn_once("sqlite-vec:not-loadable", _VEC_UNLOADABLE)
        return False
    return True


def sqlite_vec_loadable() -> bool:
    """True if the sqlite-vec extension can load in this interpreter.

    Probes the same gate the DB applies at connect time (import + runtime
    ``enable_load_extension``) on a throwaway connection. Some Python/SQLite
    builds compile out extension loading, so callers can detect up front that
    vector recall will degrade to BM25 + graph.
    """
    conn = sqlite3.connect(":memory:")
    try:
        return _load_sqlite_vec(conn)
    finally:
        conn.close()


def open_db_connection(db_path: Path) -> sqlite3.Connection:
    """A read-mostly connection to an existing index DB, with sqlite-vec loaded.

    For background readers (the vector-sidecar builder) that know only the
    file path; the caller owns the connection and closes it on its own thread.
    """
    conn = sqlite3.connect(str(db_path))
    _load_sqlite_vec(conn)
    return conn


class MemoryDB:
    """Opens/creates the per-agent index DB and owns its schema.

    One instance per agent workspace. The connection is opened lazily and the
    schema is created idempotently, so constructing a ``MemoryDB`` on an existing
    workspace is a no-op beyond opening the file.
    """

    def __init__(
        self, workspace: Path, *, dims: int = DEFAULT_DIMS, durability: Durability = "full"
    ) -> None:
        self._workspace = Path(workspace)
        self._dims = dims
        self._durability = durability
        self._db_path = self._workspace / "memory" / "index.db"
        self._conn: sqlite3.Connection | None = None
        self._vec_available = False
        self._instance_id = ""
        # Off-loop access: ONE dedicated worker thread owning ONE connection.
        # sqlite3 connections are bound to the thread that opened them
        # (check_same_thread), so the worker's connection is opened, used and
        # closed only on that thread; the loop-thread ``_conn`` is never shared.
        self._worker: ThreadPoolExecutor | None = None
        self._worker_conn: sqlite3.Connection | None = None

    @property
    def db_path(self) -> Path:
        """Absolute path to this agent's index DB file."""
        return self._db_path

    @property
    def vec_available(self) -> bool:
        """Whether the sqlite-vec extension loaded (the ``vec0`` table exists)."""
        self.connect()
        return self._vec_available

    @property
    def dims(self) -> int:
        """Embedding width the ``vec0`` table was created for."""
        return self._dims

    @property
    def instance_id(self) -> str:
        """Random id stamped into this DB file at creation.

        Distinguishes a re-created file at the same path from the old one, so a
        cached derivative (the vector sidecar) can never outlive its source.
        """
        self.connect()
        return self._instance_id

    def connect(self) -> sqlite3.Connection:
        """Open the DB (creating the file + schema on first call)."""
        if self._conn is not None:
            return self._conn

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn, self._vec_available = self._open()
        self._conn = conn
        self._create_schema(conn)
        row = conn.execute("SELECT value FROM index_meta WHERE key='instance_id'").fetchone()
        self._instance_id = str(row[0])
        return conn

    def open_connection(self) -> sqlite3.Connection:
        """A NEW connection to this DB file (pragmas + sqlite-vec), no schema work.

        The caller owns it and must use and close it on one thread. ``connect``
        uses this for the loop-thread connection; the off-loop worker and the
        vector-sidecar builder each open their own.
        """
        return self._open()[0]

    def _open(self) -> tuple[sqlite3.Connection, bool]:
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        # Under FULL each commit waits on an fsync. Only a provider-rebuildable
        # store opts into NORMAL.
        conn.execute(
            "PRAGMA synchronous=NORMAL"
            if self._durability == "normal"
            else "PRAGMA synchronous=FULL"
        )
        return conn, _load_sqlite_vec(conn)

    async def run(self, work: Callable[[sqlite3.Connection], _T]) -> _T:
        """Run ``work(conn)`` off the event loop, on this DB's worker thread.

        Every call shares one worker thread and its own connection, so calls run
        one at a time in submission order (a write is visible to the next read)
        and no connection ever crosses threads. ``work`` must finish its own
        transaction (commit or roll back) before it returns.
        """
        self.connect()  # schema exists before the worker's first statement
        if self._worker is None:
            self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="arcmemory-db")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._worker, self._run_on_worker, work)

    def _run_on_worker(self, work: Callable[[sqlite3.Connection], _T]) -> _T:
        if self._worker_conn is None:
            self._worker_conn = self.open_connection()
        return work(self._worker_conn)

    def _close_worker_conn(self) -> None:
        if self._worker_conn is not None:
            self._worker_conn.close()
            self._worker_conn = None

    def close(self) -> None:
        """Close both connections and stop the worker thread (idempotent)."""
        if self._worker is not None:
            self._worker.submit(self._close_worker_conn)
            self._worker.shutdown(wait=True)
            self._worker = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        """Create every table idempotently. Guards the vec0 virtual table."""
        # Raw episodic stream — the high-volume append-only source.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS episodic ("
            "event_id TEXT PRIMARY KEY, ts TEXT NOT NULL, scope TEXT NOT NULL, "
            "kind TEXT NOT NULL, text TEXT NOT NULL, hash TEXT, "
            "classification TEXT DEFAULT 'unclassified', refs TEXT, seq INTEGER, "
            "salience REAL NOT NULL DEFAULT 0.0, entities TEXT, source_updated_at TEXT)"
        )
        # T-702/703 added salience/entities to a table that already existed on
        # every deployed agent — CREATE TABLE IF NOT EXISTS no-ops there, so a
        # real self-migration is required (task 37). _ensure_columns is the
        # general seam: the NEXT column added to an existing table lists here
        # instead of repeating this bug. SPEC-073 added source_updated_at the same way.
        self._ensure_columns(
            conn,
            "episodic",
            {
                "salience": "REAL NOT NULL DEFAULT 0.0",
                "entities": "TEXT",
                "source_updated_at": "TEXT",
            },
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_episodic_scope ON episodic(scope, seq)")

        # Index provenance for rebuild + the no-read-up classification label.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS chunks ("
            "chunk_id TEXT PRIMARY KEY, scope TEXT NOT NULL, source_path TEXT NOT NULL, "
            "mtime REAL, classification TEXT DEFAULT 'unclassified', content_hash TEXT, "
            "fts_rowid INTEGER)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_scope ON chunks(scope)")
        # Recency is a top-k channel: this index lets ``ORDER BY ... LIMIT k``
        # read k rows instead of sorting every chunk in the scope.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_scope_recency "
            "ON chunks(scope, COALESCE(mtime, 0) DESC, chunk_id)"
        )
        # H-REG-1: ``content_hash`` tracks freshness for the CHEAP lexical write
        # (chunk row + fts/BM25 row, no embedder needed). ``embedded_hash`` tracks,
        # separately, which content_hash the VECTOR was last embedded for — so a
        # background embed pass can tell "written lexically, never embedded"
        # (embedded_hash NULL or stale) apart from "already embedded, nothing to
        # do" instead of the lexical write's hash bump masking a pending embed.
        # The backfill this migration needs runs below, AFTER vec0 exists.
        chunks_columns_added = self._ensure_columns(conn, "chunks", {"embedded_hash": "TEXT"})

        # FTS5 keyword/BM25 mirror of chunk text.
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks "
            "USING fts5(chunk_id UNINDEXED, scope UNINDEXED, text)"
        )
        self._link_fts_rowids(conn)
        # Per-term document counts, so BM25 can price a query before running it.
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks_vocab "
            "USING fts5vocab(fts_chunks, 'row')"
        )

        # Semantic + cue graph: weighted edges carrying Hebbian/decay state.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS edges ("
            "scope TEXT NOT NULL, src TEXT NOT NULL, dst TEXT NOT NULL, kind TEXT NOT NULL, "
            "weight REAL NOT NULL DEFAULT 0.0, salience REAL NOT NULL DEFAULT 0.0, "
            "last_hit TEXT, hits INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY (scope, src, dst, kind))"
        )
        # Undirected neighbor lookups match ``dst`` too; without this every
        # spreading-activation hop scanned the scope's whole edge list.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(scope, dst)")

        if self._vec_available:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS vec0 "
                f"USING vec0(chunk_id TEXT PRIMARY KEY, embedding float[{self._dims}])"
            )

        if "embedded_hash" in chunks_columns_added and self._vec_available:
            # Migration backfill (production hazard): a box upgraded from before
            # this column existed already has real vectors for rows the column
            # starts NULL on. Left unfixed, the first post-deploy embed pass
            # would see every one of those rows as "never embedded" and re-embed
            # the ENTIRE existing corpus — months of memory, a cost/latency spike
            # of the same class as the past consolidation runaway. Stamping
            # ``embedded_hash = content_hash`` for every chunk that already has a
            # vec0 row marks exactly those rows resolved, so only genuinely
            # unembedded content is pending after deploy. Gated on
            # ``chunks_columns_added`` — this branch runs only the ONE time the
            # column is actually created — so it is a no-op, safe to call, on an
            # already-migrated DB. Must run AFTER the ``vec0`` create above: on a
            # brand-new DB the table would not exist yet otherwise.
            conn.execute(
                "UPDATE chunks SET embedded_hash = content_hash "
                "WHERE chunk_id IN (SELECT chunk_id FROM vec0)"
            )
            conn.commit()

        # Per-scope vector version stamp. Every transaction that changes a
        # scope's vec0 rows bumps its generation; the HNSW sidecar records the
        # generation it reflects, so any mismatch (a crash before the sidecar
        # was saved, another writer, a rebuild) is detected and healed.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS vec_generation ("
            "scope TEXT PRIMARY KEY, generation INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO index_meta (key, value) VALUES ('instance_id', ?)",
            (uuid.uuid4().hex,),
        )

        # Abstraction-space trigger vectors — kept in a SEPARATE table from the
        # surface ``vec0`` chunks (SDD 7) so surface noise cannot drown a minted
        # trigger. A plain table (float32 blob) so the structural trigger channel
        # works even where the sqlite-vec extension is absent (it degrades to the
        # cue-graph channel only when *no embedder* is injected, not when vec0 is).
        conn.execute(
            "CREATE TABLE IF NOT EXISTS insight_trigger ("
            "insight_id TEXT PRIMARY KEY, scope TEXT NOT NULL, "
            "content_hash TEXT NOT NULL, embedding BLOB NOT NULL)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_insight_trigger_scope ON insight_trigger(scope)"
        )

        # Canonical item dedup + per-provenance classification (SPEC-073 COMP-011).
        # Same bytes from two sources form ONE item; each source's provenance
        # keeps its own classification so retrieval gates per-provenance, not
        # on the item's highest label.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS items ("
            "item_id TEXT PRIMARY KEY, content_hash TEXT NOT NULL, first_seen TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS item_provenances ("
            "item_id TEXT NOT NULL, source TEXT NOT NULL, external_id TEXT NOT NULL, "
            "classification TEXT DEFAULT 'unclassified', "
            "PRIMARY KEY(item_id, source, external_id))"
        )
        # Every connected object replaces its own provenance by (source, object):
        # without this, each one scanned every provenance row of every source.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_item_provenances_source "
            "ON item_provenances(source, external_id)"
        )

        conn.commit()

    @staticmethod
    def _link_fts_rowids(conn: sqlite3.Connection) -> None:
        """Give every chunk row the rowid of its ``fts_chunks`` text row.

        ``fts_chunks.chunk_id`` is UNINDEXED, so any lookup by it reads the
        whole text index. The rowid link makes per-chunk writes, deletes and
        point reads a B-tree lookup. A database created before the column is
        linked here once, in ONE transaction with the column itself, so a crash
        part-way leaves the old schema and the next open simply retries. Text
        rows no chunk row points at are orphans of earlier deletes and go too.
        """
        columns = {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
        if "fts_rowid" in columns:
            return
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN")
        try:
            conn.execute("ALTER TABLE chunks ADD COLUMN fts_rowid INTEGER")
            conn.execute(
                "CREATE TEMP TABLE fts_link AS "
                "SELECT MAX(rowid) AS fts_rowid, chunk_id, scope FROM fts_chunks "
                "GROUP BY chunk_id, scope"
            )
            conn.execute("CREATE INDEX temp.fts_link_key ON fts_link(chunk_id, scope)")
            conn.execute(
                "UPDATE chunks SET fts_rowid = (SELECT fts_link.fts_rowid FROM fts_link "
                "WHERE fts_link.chunk_id = chunks.chunk_id AND fts_link.scope = chunks.scope)"
            )
            conn.execute(
                "DELETE FROM fts_chunks WHERE rowid NOT IN "
                "(SELECT fts_rowid FROM chunks WHERE fts_rowid IS NOT NULL)"
            )
            conn.execute("DROP TABLE temp.fts_link")
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> set[str]:
        """Add any of ``columns`` missing from an already-existing ``table``.

        ``CREATE TABLE IF NOT EXISTS`` only applies a schema to a table that
        doesn't exist yet — a column added to that statement is silently
        absent on every DB created before the change shipped (task 37). This
        is the general seam: ``columns`` maps column name -> its DDL type/
        default, and each missing one gets ``ALTER TABLE ... ADD COLUMN``.
        A no-op migration framework on purpose — just enough to stop this
        exact failure mode from recurring on the next added column.

        Returns the subset of ``columns`` actually added this call — empty on
        an already-migrated DB — so a caller can gate a one-time backfill (run
        exactly when the column is first created, never again) on it.
        """
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        added = {name for name in columns if name not in existing}
        for name in added:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {columns[name]}")
        conn.commit()
        return added


__all__ = ["DEFAULT_DIMS", "MemoryDB", "open_db_connection"]
