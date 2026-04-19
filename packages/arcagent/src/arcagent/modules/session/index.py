"""SessionIndex -- polling FTS5 indexer for JSONL session files.

Architecture (SDD section 3.2):
  sessions/*.jsonl  -- primary store (audit-truth, written by session_manager)
      poll every poll_interval seconds
  SessionIndex
      INSERT INTO messages + trigger -> messages_fts (FTS5 external-content)
  sessions/index.db -- derived store (search reads here)

Crash-safety:
  The indexer tracks the last byte offset indexed per JSONL file in the
  sync_state table.  On restart it replays from the last committed offset.
  Partial lines at EOF are skipped until the writer finishes them.

Thread-safety design:
  SQLite connections are NOT shared across threads.  Each call site opens its
  own connection for the duration of its operation and closes it immediately
  after.  This avoids the segfault that occurs when stop() closes a connection
  while _scan_once() (running in asyncio.to_thread) still holds it open.

Performance (SPEC-018 Wave B1):
  - asyncio.create_task() replaces asyncio.get_event_loop().create_task().
  - Per-row INSERT loop in _index_file replaced with executemany() in batches
    of 500.
  - rebuild() method runs full re-index with PRAGMA synchronous=OFF and
    PRAGMA journal_mode=MEMORY.
    DURABILITY TRADEOFF: crash during rebuild may corrupt the index; delete
    index.db and re-run rebuild() to recover (JSONL files are the truth).
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from arcagent.modules.session.store import read_messages_from_offset

_logger = logging.getLogger("arcagent.modules.session.index")

# Batch size for executemany() inserts.
_INSERT_BATCH_SIZE = 500

_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_did TEXT,
    agent_did TEXT,
    classification TEXT,
    role TEXT,
    ts REAL,
    content TEXT,
    jsonl_path TEXT NOT NULL,
    jsonl_offset INTEGER NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content=messages,
    content_rowid=id,
    tokenize='porter unicode61 remove_diacritics 2',
    columnsize=0
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TABLE IF NOT EXISTS sync_state(
    jsonl_path TEXT PRIMARY KEY,
    offset INTEGER NOT NULL DEFAULT 0,
    last_hash TEXT
);
"""


class SearchHit(BaseModel):
    """A single full-text search result with positional context."""

    session_id: str
    role: str
    ts: float
    snippet: str
    jsonl_path: str
    jsonl_offset: int
    user_did: str
    agent_did: str
    classification: str


_INSERT_SQL = (
    "INSERT INTO messages("
    "session_id, user_did, agent_did, classification, "
    "role, ts, content, jsonl_path, jsonl_offset"
    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

class SessionIndex:
    """Polling indexer: reads JSONL files, writes FTS5 index.

    Public API:
      await index.start()    -- initialises schema, starts background poll loop
      await index.stop()     -- cancels poll loop
      await index.rebuild()  -- full re-index with fast MEMORY journal (see note)
      index.search(...)      -- synchronous FTS5 query (opens its own connection)
    """

    def __init__(
        self,
        db_path: Path,
        sessions_dir: Path,
        poll_interval: float = 30.0,
    ) -> None:
        self._db_path = db_path
        self._sessions_dir = sessions_dir
        self._poll_interval = poll_interval
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Ensure schema exists, start background poll loop."""
        await asyncio.to_thread(self._init_schema)
        self._stop_event.clear()
        self._started = True
        # Use asyncio.create_task() directly (replaces deprecated
        # asyncio.get_event_loop() plus .create_task() is the deprecated form.
        self._task = asyncio.create_task(
            self._poll_loop(), name="session_index_poll"
        )
        _logger.info(
            "SessionIndex started: db=%s sessions=%s interval=%.1fs",
            self._db_path,
            self._sessions_dir,
            self._poll_interval,
        )

    async def stop(self) -> None:
        """Signal the poll loop to stop and wait for it to finish."""
        self._stop_event.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._started = False
        _logger.info("SessionIndex stopped")

    async def rebuild(self) -> None:
        """Full re-index with fast MEMORY journal mode.

        DURABILITY TRADEOFF: not crash-safe during rebuild.
        """
        await asyncio.to_thread(self._rebuild_sync)

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Poll JSONL files every poll_interval seconds until stopped."""
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._stop_event.wait()),
                    timeout=self._poll_interval,
                )
                break
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                break

            try:
                await asyncio.to_thread(self._scan_once)
            except Exception:
                _logger.exception("SessionIndex._scan_once raised unexpectedly")

    # ------------------------------------------------------------------
    # Schema initialisation
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA_SQL)
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def _scan_once(self) -> None:
        """Index all new lines from every JSONL file in sessions_dir."""
        conn = self._connect()
        try:
            for path in iter_session_files_from(self._sessions_dir):
                try:
                    self._index_file(conn, path)
                except Exception:
                    _logger.exception("Failed to index %s", path)
        finally:
            conn.close()

    def _index_file(self, conn: sqlite3.Connection, path: Path) -> None:
        """Index new lines using executemany batching (_INSERT_BATCH_SIZE)."""
        path_str = str(path)
        cur = conn.execute(
            "SELECT offset FROM sync_state WHERE jsonl_path = ?", (path_str,)
        )
        row = cur.fetchone()
        start_offset: int = row[0] if row else 0

        entries, new_offset = read_messages_from_offset(path, start_offset)

        if row is None:
            conn.execute(
                "INSERT OR IGNORE INTO sync_state(jsonl_path, offset) VALUES (?, 0)",
                (path_str,),
            )
            conn.commit()

        if not entries:
            return

        session_id = path.stem

        rows = [
            r
            for entry in entries
            if (r := _entry_to_row(session_id, path_str, start_offset, entry)) is not None
        ]

        if not rows:
            return

        with conn:
            for i in range(0, len(rows), _INSERT_BATCH_SIZE):
                conn.executemany(_INSERT_SQL, rows[i : i + _INSERT_BATCH_SIZE])

            conn.execute(
                """
                INSERT INTO sync_state(jsonl_path, offset)
                VALUES (?, ?)
                ON CONFLICT(jsonl_path) DO UPDATE SET offset = excluded.offset
                """,
                (path_str, new_offset),
            )

    # ------------------------------------------------------------------
    # Rebuild
    # ------------------------------------------------------------------

    def _rebuild_sync(self) -> None:
        """Blocking implementation of rebuild(). Called via asyncio.to_thread."""
        conn = self._connect()
        try:
            conn.execute("PRAGMA synchronous=OFF")
            conn.execute("PRAGMA journal_mode=MEMORY")

            conn.executescript(
                """
                DELETE FROM messages;
                DELETE FROM messages_fts;
                DELETE FROM sync_state;
                """
            )
            conn.commit()

            for path in iter_session_files_from(self._sessions_dir):
                try:
                    self._index_file(conn, path)
                except Exception:
                    _logger.exception("rebuild: failed to index %s", path)

        finally:
            try:
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.commit()
            except Exception:
                _logger.exception("rebuild: failed to restore durable pragmas")
            conn.close()

        _logger.info("SessionIndex.rebuild() complete: db=%s", self._db_path)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        q: str,
        limit: int = 20,
        since: datetime | None = None,
        classification_max: str | None = None,
    ) -> list[SearchHit]:
        """Full-text search over the FTS5 index."""
        if not self._started:
            return []

        if not self._db_path.exists():
            return []

        conn = self._connect()
        try:
            return _run_search(conn, q, limit, since, classification_max)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(
            str(self._db_path),
            check_same_thread=True,
            timeout=5.0,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

_CLASSIFICATION_ORDER = ["unclassified", "cui", "secret"]


def _classifications_up_to(max_level: str) -> list[str]:
    max_level = max_level.lower()
    try:
        idx = _CLASSIFICATION_ORDER.index(max_level)
    except ValueError:
        idx = 0
    return _CLASSIFICATION_ORDER[: idx + 1]


def _entry_to_row(
    session_id: str,
    path_str: str,
    file_offset: int,
    entry: dict[str, Any],
) -> tuple[str, str, str, str, str, float, str, str, int] | None:
    """Convert a JSONL entry dict to an INSERT row tuple, or None to skip."""
    content = entry.get("content")
    if not content:
        return None
    if not isinstance(content, str):
        try:
            content = _json.dumps(content)
        except Exception:
            return None

    ts_raw = entry.get("timestamp", "")
    ts: float = 0.0
    if ts_raw:
        try:
            ts = datetime.fromisoformat(ts_raw).timestamp()
        except ValueError:
            pass

    return (
        session_id,
        entry.get("user_did", ""),
        entry.get("agent_did", ""),
        entry.get("classification", "unclassified"),
        entry.get("role", ""),
        ts,
        content,
        path_str,
        file_offset,
    )


def _insert_entry(
    conn: sqlite3.Connection,
    session_id: str,
    path_str: str,
    file_offset: int,
    entry: dict[str, Any],
) -> None:
    """Insert a single JSONL entry. Kept for backward compatibility."""
    row = _entry_to_row(session_id, path_str, file_offset, entry)
    if row is None:
        return

    conn.execute(
        """
        INSERT INTO messages(
            session_id, user_did, agent_did, classification,
            role, ts, content, jsonl_path, jsonl_offset
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        row,
    )


def _run_search(
    conn: sqlite3.Connection,
    q: str,
    limit: int,
    since: datetime | None,
    classification_max: str | None,
) -> list[SearchHit]:
    filters: list[str] = []
    params: list[Any] = [q]

    if since is not None:
        filters.append("m.ts >= ?")
        params.append(since.timestamp())

    if classification_max is not None:
        allowed = _classifications_up_to(classification_max)
        placeholders = ",".join("?" * len(allowed))
        filters.append(f"m.classification IN ({placeholders})")
        params.extend(allowed)

    params.append(limit)

    _base_sql = (
        "SELECT "
        "    m.session_id, "
        "    m.role, "
        "    m.ts, "
        "    snippet(messages_fts, 0, '<<', '>>', '...', 20) AS snippet, "
        "    m.jsonl_path, "
        "    m.jsonl_offset, "
        "    COALESCE(m.user_did, '') AS user_did, "
        "    COALESCE(m.agent_did, '') AS agent_did, "
        "    COALESCE(m.classification, 'unclassified') AS classification "
        "FROM messages_fts "
        "JOIN messages m ON messages_fts.rowid = m.id "
        "WHERE messages_fts MATCH ? "
    )
    _filter_clause = ("AND " + " AND ".join(filters) + " ") if filters else ""
    sql = _base_sql + _filter_clause + "ORDER BY rank LIMIT ?"

    try:
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
    except sqlite3.OperationalError:
        _logger.exception("FTS5 search query failed")
        return []

    return [
        SearchHit(
            session_id=row[0],
            role=row[1] or "",
            ts=row[2] or 0.0,
            snippet=row[3] or "",
            jsonl_path=row[4],
            jsonl_offset=row[5],
            user_did=row[6],
            agent_did=row[7],
            classification=row[8],
        )
        for row in rows
    ]


def iter_session_files_from(sessions_dir: Path) -> list[Path]:
    """List JSONL session files from an arbitrary sessions dir."""
    if not sessions_dir.exists():
        return []
    return sorted(sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
