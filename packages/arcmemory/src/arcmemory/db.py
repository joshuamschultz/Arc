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
            "kind TEXT NOT NULL, text TEXT NOT NULL, hash TEXT, "
            "classification TEXT DEFAULT 'unclassified', refs TEXT, seq INTEGER, "
            "salience REAL NOT NULL DEFAULT 0.0, entities TEXT)"
        )
        # T-702/703 added salience/entities to a table that already existed on
        # every deployed agent — CREATE TABLE IF NOT EXISTS no-ops there, so a
        # real self-migration is required (task 37). _ensure_columns is the
        # general seam: the NEXT column added to an existing table lists here
        # instead of repeating this bug.
        self._ensure_columns(
            conn, "episodic", {"salience": "REAL NOT NULL DEFAULT 0.0", "entities": "TEXT"}
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

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
        """Add any of ``columns`` missing from an already-existing ``table``.

        ``CREATE TABLE IF NOT EXISTS`` only applies a schema to a table that
        doesn't exist yet — a column added to that statement is silently
        absent on every DB created before the change shipped (task 37). This
        is the general seam: ``columns`` maps column name -> its DDL type/
        default, and each missing one gets ``ALTER TABLE ... ADD COLUMN``.
        A no-op migration framework on purpose — just enough to stop this
        exact failure mode from recurring on the next added column.
        """
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        conn.commit()


__all__ = ["DEFAULT_DIMS", "MemoryDB"]
