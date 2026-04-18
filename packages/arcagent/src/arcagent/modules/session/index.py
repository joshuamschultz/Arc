"""SessionIndex — polling FTS5 indexer for JSONL session files.

Architecture (SDD §3.2):
  sessions/*.jsonl  ← primary store (audit-truth, written by session_manager)
      ↑ poll every poll_interval seconds
  SessionIndex
      ↓ INSERT INTO messages + trigger → messages_fts (FTS5 external-content)
  sessions/index.db ← derived store (search reads here)

Crash-safety:
  The indexer tracks the last byte offset indexed per JSONL file in the
  sync_state table.  On restart it replays from the last committed offset,
  so no messages are lost and no messages are double-counted (idempotent
  replay).  Partial lines at EOF are skipped until the writer finishes them.

Thread-safety design:
  SQLite connections are NOT shared across threads.  Each call site opens its
  own connection for the duration of its operation and closes it immediately
  after.  This avoids the segfault that occurs when stop() closes a connection
  while _scan_once() (running in asyncio.to_thread) still holds it open.

WAL mode is enabled so readers (session_search) never block the indexer writer.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from arcagent.modules.session.store import read_messages_from_offset

_logger = logging.getLogger("arcagent.modules.session.index")

# SQLite schema.  External-content FTS5 keeps snippet() / highlight() working.
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


class SessionIndex:
    """Polling indexer: reads JSONL files, writes FTS5 index.

    Public API:
      await index.start()   — initialises schema, starts background poll loop
      await index.stop()    — cancels poll loop
      index.search(...)     — synchronous FTS5 query (opens its own connection)

    Thread-safety: every blocking DB operation (scan_once, search) opens a
    fresh sqlite3 connection and closes it before returning.  No connection is
    shared across threads or stored as long-lived instance state.
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
        # Initialise schema once (blocking, but only at startup).
        await asyncio.to_thread(self._init_schema)
        self._stop_event.clear()
        self._started = True
        self._task = asyncio.get_event_loop().create_task(
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

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Poll JSONL files every poll_interval seconds until stopped.

        Sleep-first pattern: wait poll_interval before scanning.  This ensures
        that unit tests that call _scan_once manually (after start()) see no
        double-counting.  Production use cases with short poll_interval still
        see near-real-time indexing.  To scan immediately on startup, callers
        can call _scan_once directly before start() if needed.
        """
        while not self._stop_event.is_set():
            # Sleep first, scan after interval (or on stop signal).
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._stop_event.wait()),
                    timeout=self._poll_interval,
                )
                break  # stop_event fired; exit without scanning
            except TimeoutError:
                pass  # Normal: interval elapsed; proceed to scan
            except asyncio.CancelledError:
                break

            try:
                await asyncio.to_thread(self._scan_once)
            except Exception:
                _logger.exception("SessionIndex._scan_once raised unexpectedly")

    # ------------------------------------------------------------------
    # Schema initialisation (called once at startup from to_thread)
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        """Open a fresh connection, apply the schema, then close."""
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA_SQL)
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Scanning (called from asyncio.to_thread — may use blocking sqlite3)
    # ------------------------------------------------------------------

    def _scan_once(self) -> None:
        """Index all new lines from every JSONL file in sessions_dir.

        Opens its own connection for the duration of the scan and closes it
        before returning — no shared connection state with the caller thread.
        """
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
        """Index new lines from a single JSONL file.

        Reads the committed byte offset from sync_state, reads only new
        complete lines, inserts messages, then commits the new offset.
        The FTS5 trigger fires automatically on each INSERT.
        """
        path_str = str(path)
        cur = conn.execute(
            "SELECT offset FROM sync_state WHERE jsonl_path = ?", (path_str,)
        )
        row = cur.fetchone()
        start_offset: int = row[0] if row else 0

        entries, new_offset = read_messages_from_offset(path, start_offset)

        # Ensure the file is tracked in sync_state even if no new entries.
        if row is None:
            conn.execute(
                "INSERT OR IGNORE INTO sync_state(jsonl_path, offset) VALUES (?, 0)",
                (path_str,),
            )
            conn.commit()

        if not entries:
            return

        # Derive session_id from filename (UUID4 stem).
        session_id = path.stem

        # A single transaction per file: crash between files is fine; the
        # last committed file offset is the crash-recovery checkpoint.
        with conn:
            for entry in entries:
                _insert_entry(conn, session_id, path_str, start_offset, entry)

            conn.execute(
                """
                INSERT INTO sync_state(jsonl_path, offset)
                VALUES (?, ?)
                ON CONFLICT(jsonl_path) DO UPDATE SET offset = excluded.offset
                """,
                (path_str, new_offset),
            )

    # ------------------------------------------------------------------
    # Search (synchronous, opens own connection)
    # ------------------------------------------------------------------

    def search(
        self,
        q: str,
        limit: int = 20,
        since: datetime | None = None,
        classification_max: str | None = None,
    ) -> list[SearchHit]:
        """Full-text search over the FTS5 index.

        Parameters
        ----------
        q:
            FTS5 query string (supports phrase queries, NOT, AND, OR, NEAR).
        limit:
            Maximum number of hits to return.
        since:
            If provided, only return messages with ts >= since.timestamp().
        classification_max:
            ACL stub — excludes messages whose classification level exceeds
            this value.  Supported values: 'unclassified', 'cui', 'secret'.
            Full ACL enforcement is M2 work; this path exercises the query
            filter so tests can verify it is plumbed correctly.
            TODO (M2 work): replace stub with full memory_acl module integration.

        Returns
        -------
        list[SearchHit]
            Ranked by FTS5 BM25 score (best match first).
        """
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
        """Open a new SQLite connection.  Caller is responsible for closing."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(
            str(self._db_path),
            check_same_thread=True,  # explicit: each thread opens its own
            timeout=5.0,
        )


# ---------------------------------------------------------------------------
# Module-level helpers (free functions to keep SessionIndex methods focused)
# ---------------------------------------------------------------------------

_CLASSIFICATION_ORDER = ["unclassified", "cui", "secret"]


def _classifications_up_to(max_level: str) -> list[str]:
    """Return all classification levels at or below max_level."""
    max_level = max_level.lower()
    try:
        idx = _CLASSIFICATION_ORDER.index(max_level)
    except ValueError:
        # Unknown level — default to most restrictive (unclassified only).
        idx = 0
    return _CLASSIFICATION_ORDER[: idx + 1]


def _insert_entry(
    conn: sqlite3.Connection,
    session_id: str,
    path_str: str,
    file_offset: int,
    entry: dict[str, Any],
) -> None:
    """Insert a single JSONL entry into the messages table.

    Only inserts entries that have a 'content' field (i.e., message
    entries, not compaction_summary or other metadata entries).
    """
    content = entry.get("content")
    if not content:
        return
    if not isinstance(content, str):
        # content may be a list (tool-use blocks); stringify for search.
        try:
            import json as _json

            content = _json.dumps(content)
        except Exception:
            return

    ts_raw = entry.get("timestamp", "")
    ts: float = 0.0
    if ts_raw:
        try:
            ts = datetime.fromisoformat(ts_raw).timestamp()
        except ValueError:
            pass

    conn.execute(
        """
        INSERT INTO messages(
            session_id, user_did, agent_did, classification,
            role, ts, content, jsonl_path, jsonl_offset
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            entry.get("user_did", ""),
            entry.get("agent_did", ""),
            entry.get("classification", "unclassified"),
            entry.get("role", ""),
            ts,
            content,
            path_str,
            file_offset,
        ),
    )


def _run_search(
    conn: sqlite3.Connection,
    q: str,
    limit: int,
    since: datetime | None,
    classification_max: str | None,
) -> list[SearchHit]:
    """Execute the FTS5 search query and return SearchHit results."""
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

    # Build SQL from constant parts joined together.
    # Security note: the WHERE clause additions use only constant string
    # fragments and IN placeholders ("?"); no user-supplied values appear
    # directly in the SQL text — all values flow through parameterised bindings.
    _base_sql = (
        "SELECT "
        "    m.session_id, "
        "    m.role, "
        "    m.ts, "
        "    snippet(messages_fts, 0, '<<', '>>', '…', 20) AS snippet, "
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
    """List JSONL session files from an arbitrary sessions dir.

    Thin adapter so _scan_once can pass its own sessions_dir without
    importing the workspace-aware version from store.py.
    """
    if not sessions_dir.exists():
        return []
    return sorted(sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
