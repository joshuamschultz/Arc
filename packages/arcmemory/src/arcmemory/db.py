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

import sqlite3
from pathlib import Path

try:  # optional [vec] extra — guarded, never fatal
    import sqlite_vec

    _SQLITE_VEC_IMPORTABLE = True
except ImportError:  # pragma: no cover - exercised only where the extra is absent
    _SQLITE_VEC_IMPORTABLE = False

# Default embedding width (bge-small / MiniLM are both 384-dim).
DEFAULT_DIMS = 384

# The derived tables that a rebuild wipes and re-derives. Kept as a constant so
# rebuild and tests share one source of truth for "what is disposable". The
# ``insight_trigger`` abstraction-space vectors are derived from the insight cards
# (re-embedded by ``index/structural.trigger_index``), hence disposable too.
DERIVED_TABLES = ("fts_chunks", "edges", "vec0", "insight_trigger")


def _load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Attempt to load the sqlite-vec extension onto ``conn``.

    Returns True on success. Guarded on both the import and the runtime
    ``enable_load_extension`` capability (some Python builds compile it out),
    so a missing extension degrades instead of crashing.
    """
    if not _SQLITE_VEC_IMPORTABLE:
        return False
    if not hasattr(conn, "enable_load_extension"):
        return False
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except (sqlite3.OperationalError, AttributeError):
        return False
    return True


class MemoryDB:
    """Opens/creates the per-agent index DB and owns its schema.

    One instance per agent workspace. The connection is opened lazily and the
    schema is created idempotently, so constructing a ``MemoryDB`` on an existing
    workspace is a no-op beyond opening the file.
    """

    def __init__(self, workspace: Path, *, dims: int = DEFAULT_DIMS) -> None:
        self._workspace = Path(workspace)
        self._dims = dims
        self._db_path = self._workspace / "memory" / "index.db"
        self._conn: sqlite3.Connection | None = None
        self._vec_available = False

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

    def connect(self) -> sqlite3.Connection:
        """Open the DB (creating the file + schema on first call)."""
        if self._conn is not None:
            return self._conn

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        self._vec_available = _load_sqlite_vec(conn)
        self._conn = conn
        self._create_schema(conn)
        return conn

    def close(self) -> None:
        """Close the connection (idempotent)."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        """Create every table idempotently. Guards the vec0 virtual table."""
        # Raw episodic stream — the high-volume append-only source.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS episodic ("
            "event_id TEXT PRIMARY KEY, ts TEXT NOT NULL, scope TEXT NOT NULL, "
            "kind TEXT NOT NULL, text TEXT NOT NULL, hash TEXT, refs TEXT, seq INTEGER)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_episodic_scope ON episodic(scope, seq)")

        # Index provenance for rebuild + the no-read-up classification label.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS chunks ("
            "chunk_id TEXT PRIMARY KEY, scope TEXT NOT NULL, source_path TEXT NOT NULL, "
            "mtime REAL, classification TEXT DEFAULT 'unclassified', content_hash TEXT)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_scope ON chunks(scope)")

        # FTS5 keyword/BM25 mirror of chunk text.
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks "
            "USING fts5(chunk_id UNINDEXED, scope UNINDEXED, text)"
        )

        # Semantic + cue graph: weighted edges carrying Hebbian/decay state.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS edges ("
            "scope TEXT NOT NULL, src TEXT NOT NULL, dst TEXT NOT NULL, kind TEXT NOT NULL, "
            "weight REAL NOT NULL DEFAULT 0.0, salience REAL NOT NULL DEFAULT 0.0, "
            "last_hit TEXT, hits INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY (scope, src, dst, kind))"
        )

        if self._vec_available:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS vec0 "
                f"USING vec0(chunk_id TEXT PRIMARY KEY, embedding float[{self._dims}])"
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

        conn.commit()

    def wipe_derived(self) -> None:
        """Drop every disposable derived table (rebuild precondition).

        The ``episodic`` raw stream and ``chunks`` provenance are preserved; only
        FTS5 / vectors / graph edges are cleared, to be re-derived from truth.
        """
        conn = self.connect()
        conn.execute("DELETE FROM fts_chunks")
        conn.execute("DELETE FROM edges")
        conn.execute("DELETE FROM insight_trigger")
        if self._vec_available:
            conn.execute("DELETE FROM vec0")
        conn.commit()


__all__ = ["DEFAULT_DIMS", "DERIVED_TABLES", "MemoryDB"]
