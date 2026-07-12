"""SqliteBackend — the default StorageBackend (WAL, shared-nothing per instance).

Clones the proven ``arcagent.modules.session.index.SessionIndex`` pattern
(research §11.3): WAL journal, ``busy_timeout`` (closes defect C5), per-operation
connections (never shared across threads), ``executemany`` batching, and
``INSERT OR IGNORE`` idempotency keyed on the content-derived ``record_id`` —
never a byte offset or rowid. The blocking ``sqlite3`` work is bridged off the
event loop with ``asyncio.to_thread`` so the Protocol stays async.

Each instance owns its **own** DB file (NFR-8 shared-nothing) — a shared file
across instances produces ``SQLITE_BUSY`` storms above ~2-3 concurrent writers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arctrust.audit import AuditEvent, emit

from arcstore.backends.base import (
    AUDIT_TABLE,
    MUTABLE_RECORDS_TABLE,
    OPERATIONAL_TABLES,
    SKILL_BODIES_TABLE,
    SKILL_CANDIDATES_TABLE,
)

_logger = logging.getLogger("arcstore.backends.sqlite")

_BUSY_TIMEOUT_MS = 5000
_BATCH_SIZE = 500

_OPERATIONAL_COLUMNS = (
    "record_id",
    "kind",
    "actor_did",
    "ts",
    "request_id",
    "model",
    "provider",
    "agent_label",
    "prompt_tokens",
    "completion_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost_usd",
    "latency_ms",
    "outcome",
    "name",
    # tool_event columns (SPEC-028 FR-1/FR-2)
    "tool_name",
    "phase",
    "args_digest",
    "args_size",
    "result_digest",
    "result_size",
    # spawn_event columns (SPEC-028 FR-3)
    "parent_did",
    "child_did",
    "role",
    "depth",
    "extra",
)
_AUDIT_COLUMNS = (
    "record_id",
    "seq",
    "ts",
    "actor_did",
    "action",
    "target",
    "outcome",
    "event_hash",
    "prev_hash",
    "signature",
    "verified",
)
# The arcskill candidate-store mirror (SPEC-054 REQ-120). ``body_hash`` NULL
# marks a pending/pruned body (manifest-present, file-absent).
_SKILL_CANDIDATE_COLUMNS = (
    "record_id",
    "skill_name",
    "candidate_id",
    "generation",
    "parent_id",
    "scores",
    "active",
    "body_hash",
    "ts",
)
_SKILL_BODY_COLUMNS = ("record_id", "body")

# Columns that carry structured JSON (stored as TEXT, decoded on read).
_JSON_COLUMNS = frozenset({"extra", "scores"})
# Columns stored as INTEGER but exposed as bool.
_BOOL_COLUMNS = frozenset({"verified", "active"})
# SQLite affinities for ALTER TABLE reconciliation (TEXT is the default).
_INTEGER_COLUMNS = frozenset(
    {
        "prompt_tokens",
        "completion_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "args_size",
        "result_size",
        "depth",
        "seq",
        "verified",
        "generation",
        "active",
    }
)
_REAL_COLUMNS = frozenset({"cost_usd", "latency_ms"})


def _column_sql_type(col: str) -> str:
    if col in _INTEGER_COLUMNS:
        return "INTEGER"
    if col in _REAL_COLUMNS:
        return "REAL"
    return "TEXT"


_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    **{t: _OPERATIONAL_COLUMNS for t in OPERATIONAL_TABLES},
    AUDIT_TABLE: _AUDIT_COLUMNS,
    SKILL_CANDIDATES_TABLE: _SKILL_CANDIDATE_COLUMNS,
    SKILL_BODIES_TABLE: _SKILL_BODY_COLUMNS,
}


def _operational_ddl(table: str) -> str:
    return f"""
    CREATE TABLE IF NOT EXISTS {table}(
        record_id TEXT PRIMARY KEY,
        kind TEXT,
        actor_did TEXT,
        ts TEXT,
        request_id TEXT,
        model TEXT,
        provider TEXT,
        agent_label TEXT,
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        cache_read_tokens INTEGER,
        cache_write_tokens INTEGER,
        cost_usd REAL,
        latency_ms REAL,
        outcome TEXT,
        name TEXT,
        tool_name TEXT,
        phase TEXT,
        args_digest TEXT,
        args_size INTEGER,
        result_digest TEXT,
        result_size INTEGER,
        parent_did TEXT,
        child_did TEXT,
        role TEXT,
        depth INTEGER,
        extra TEXT
    );
    """


_SCHEMA_SQL = (
    "".join(_operational_ddl(t) for t in OPERATIONAL_TABLES)
    + f"""
    CREATE TABLE IF NOT EXISTS {AUDIT_TABLE}(
        record_id TEXT PRIMARY KEY,
        seq INTEGER,
        ts TEXT,
        actor_did TEXT,
        action TEXT,
        target TEXT,
        outcome TEXT,
        event_hash TEXT,
        prev_hash TEXT,
        signature TEXT,
        verified INTEGER
    );
    CREATE TABLE IF NOT EXISTS {SKILL_CANDIDATES_TABLE}(
        record_id TEXT PRIMARY KEY,
        skill_name TEXT,
        candidate_id TEXT,
        generation INTEGER,
        parent_id TEXT,
        scores TEXT,
        active INTEGER,
        body_hash TEXT,
        ts TEXT
    );
    CREATE TABLE IF NOT EXISTS {SKILL_BODIES_TABLE}(
        record_id TEXT PRIMARY KEY,
        body TEXT
    );
    CREATE TABLE IF NOT EXISTS {MUTABLE_RECORDS_TABLE}(
        collection TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT,
        updated_at TEXT,
        PRIMARY KEY (collection, key)
    );
    CREATE TABLE IF NOT EXISTS sync_state(
        source TEXT PRIMARY KEY,
        offset INTEGER NOT NULL DEFAULT 0
    );
    """
)


class SqliteBackend:
    """SQLite-backed StorageBackend. One file per instance (shared-nothing)."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)

    async def start(self) -> None:
        await asyncio.to_thread(self._init_schema)

    async def stop(self) -> None:
        # Connections are opened and closed per operation; nothing to release.
        return None

    # -- write path --------------------------------------------------------

    async def upsert(self, table: str, key: str, row: dict[str, Any]) -> None:
        await self.upsert_many(table, [(key, row)])

    async def upsert_many(self, table: str, rows: list[tuple[str, dict[str, Any]]]) -> None:
        if not rows:
            return
        columns = _columns_for(table)
        tuples = [_row_tuple(columns, key, row) for key, row in rows]
        await asyncio.to_thread(self._write_batch, table, columns, tuples)

    # -- read path ---------------------------------------------------------

    async def query(
        self,
        table: str,
        *,
        where: dict[str, Any] | None = None,
        ts_gte: str | None = None,
        order_by: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        columns = _columns_for(table)
        return await asyncio.to_thread(self._read, table, columns, where, ts_gte, order_by, limit)

    # -- cursor ------------------------------------------------------------

    async def get_cursor(self, name: str) -> int:
        return await asyncio.to_thread(self._get_cursor, name)

    async def set_cursor(self, name: str, value: int) -> None:
        await asyncio.to_thread(self._set_cursor, name, value)

    # -- mutable directory plane (SPEC-056 Phase 0A) ------------------------

    async def mutable_write(
        self,
        collection: str,
        key: str,
        value: dict[str, Any],
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> None:
        """Upsert ``value`` under ``(collection, key)``, bumping ``updated_at``."""
        await asyncio.to_thread(self._mutable_write, collection, key, value)
        self._emit_mutable_audit(
            action="mutable.write", target=f"{collection}/{key}", actor_did=actor_did, sink=sink
        )

    async def mutable_read(self, collection: str, key: str) -> dict[str, Any] | None:
        """Return the value at ``(collection, key)`` merged with ``updated_at``, or None."""
        return await asyncio.to_thread(self._mutable_read, collection, key)

    async def mutable_delete(
        self, collection: str, key: str, *, actor_did: str, sink: Any | None = None
    ) -> bool:
        """Delete the row at ``(collection, key)``. Returns True if a row was removed."""
        deleted = await asyncio.to_thread(self._mutable_delete, collection, key)
        self._emit_mutable_audit(
            action="mutable.delete", target=f"{collection}/{key}", actor_did=actor_did, sink=sink
        )
        return deleted

    async def mutable_query(
        self, collection: str, *, where: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Return every value in ``collection`` (optionally filtered by ``where``)."""
        return await asyncio.to_thread(self._mutable_query, collection, where)

    async def update_if(
        self,
        collection: str,
        key: str,
        patch: dict[str, Any],
        where: dict[str, Any],
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> bool:
        """Atomically merge ``patch`` into the row iff it currently matches ``where``.

        Single-statement ``UPDATE ... WHERE`` under ``BEGIN IMMEDIATE`` (the
        pairing.py:788-794 claim pattern) — the match check and the write happen
        inside SQLite's own atomic step, so two concurrent callers racing the
        same key can never both observe a match and both win (unlike a
        read-then-write claim, which is not atomic across the read/write gap).
        """
        won = await asyncio.to_thread(self._update_if, collection, key, patch, where)
        self._emit_mutable_audit(
            action="mutable.update_if",
            target=f"{collection}/{key}",
            actor_did=actor_did,
            sink=sink,
        )
        return won

    def _emit_mutable_audit(self, *, action: str, target: str, actor_did: str, sink: Any) -> None:
        """Emit an AU-2/AU-3 AuditEvent for a mutable-plane write. Fail-open (AU-5)."""
        if sink is None:
            return
        try:
            event = AuditEvent(
                actor_did=actor_did, action=action, target=target, outcome="applied"
            )
            emit(event, sink)
        except Exception:  # reason: fail-open — audit must never break the write path
            _logger.warning(
                "Failed to emit AuditEvent action=%s target=%s — swallowing (AU-5)",
                action,
                target,
                exc_info=True,
            )

    # -- blocking implementations (run via asyncio.to_thread) --------------

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), check_same_thread=True, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_size_limit=67108864")  # 64 MB
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA_SQL)
            self._reconcile_columns(conn)
            conn.commit()
        finally:
            conn.close()

    def _reconcile_columns(self, conn: sqlite3.Connection) -> None:
        """Add any allowlisted column missing from a pre-existing table.

        ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so a DB
        created by an earlier schema (e.g. before the SPEC-028 tool/spawn
        columns) lacks the new columns and every SELECT listing them fails with
        ``no such column``. Forward-only ``ALTER TABLE ADD COLUMN`` keeps the
        derived store self-healing without dropping data. Table and column names
        are allowlisted constants — no injection vector.
        """
        for table, columns in _TABLE_COLUMNS.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for col in columns:
                if col not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {_column_sql_type(col)}")

    def _write_batch(
        self, table: str, columns: tuple[str, ...], tuples: list[tuple[Any, ...]]
    ) -> None:
        placeholders = ",".join("?" * len(columns))
        cols = ",".join(columns)
        # table + columns are allowlisted constants (_columns_for raises on any
        # unknown table); values are bound parameters — no injection vector.
        sql = f"INSERT OR IGNORE INTO {table}({cols}) VALUES ({placeholders})"  # noqa: S608
        conn = self._connect()
        try:
            # BEGIN IMMEDIATE acquires the write lock up front, avoiding the
            # lock-upgrade SQLITE_BUSY that ignores busy_timeout.
            conn.execute("BEGIN IMMEDIATE")
            for i in range(0, len(tuples), _BATCH_SIZE):
                conn.executemany(sql, tuples[i : i + _BATCH_SIZE])
            conn.commit()
        finally:
            conn.close()

    def _read(
        self,
        table: str,
        columns: tuple[str, ...],
        where: dict[str, Any] | None,
        ts_gte: str | None,
        order_by: str | None,
        limit: int | None,
    ) -> list[dict[str, Any]]:
        sql = f"SELECT {','.join(columns)} FROM {table}"  # noqa: S608 — table/columns are allowlisted
        params: list[Any] = []
        clauses = []
        if where:
            for col, val in where.items():
                _require_column(table, columns, col)
                clauses.append(f"{col}=?")
                params.append(_encode(col, val))
        if ts_gte is not None:
            _require_column(table, columns, "ts")
            clauses.append("ts>=?")
            params.append(ts_gte)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        if order_by:
            sql += " ORDER BY " + _safe_order_by(table, columns, order_by)
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(sql, params)
            return [_decode_row(columns, r) for r in cur.fetchall()]
        finally:
            conn.close()

    def _get_cursor(self, name: str) -> int:
        conn = self._connect()
        try:
            cur = conn.execute("SELECT offset FROM sync_state WHERE source=?", (name,))
            row = cur.fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def _set_cursor(self, name: str, value: int) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO sync_state(source, offset) VALUES (?, ?) "
                "ON CONFLICT(source) DO UPDATE SET offset=excluded.offset",
                (name, value),
            )
            conn.commit()
        finally:
            conn.close()

    def _mutable_write(self, collection: str, key: str, value: dict[str, Any]) -> None:
        now = datetime.now(UTC).isoformat()
        payload = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
        conn = self._connect()
        try:
            # BEGIN IMMEDIATE up front (see _write_batch) so this upsert never
            # hits the lock-upgrade SQLITE_BUSY that ignores busy_timeout.
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO mutable_records(collection, key, value, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(collection, key) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at",
                (collection, key, payload, now),
            )
            conn.commit()
        finally:
            conn.close()

    def _mutable_read(self, collection: str, key: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT value, updated_at FROM mutable_records WHERE collection=? AND key=?",
                (collection, key),
            ).fetchone()
            return _decode_mutable_row(row) if row is not None else None
        finally:
            conn.close()

    def _mutable_delete(self, collection: str, key: str) -> bool:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "DELETE FROM mutable_records WHERE collection=? AND key=?", (collection, key)
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def _mutable_query(
        self, collection: str, where: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        sql = "SELECT value, updated_at FROM mutable_records WHERE collection=?"
        params: list[Any] = [collection]
        if where:
            clause, where_params = _json_where(where)
            sql += " AND " + clause
            params.extend(where_params)
        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
            return [_decode_mutable_row(r) for r in rows]
        finally:
            conn.close()

    def _update_if(
        self,
        collection: str,
        key: str,
        patch: dict[str, Any],
        where: dict[str, Any],
    ) -> bool:
        clause, where_params = _json_where(where)
        # clause is built from _json_where, which only ever splices bound-param
        # placeholders (json_extract(value, ?)) — no caller value reaches SQL text.
        sql = (
            "UPDATE mutable_records SET value=json_patch(value, ?), updated_at=? "  # noqa: S608
            f"WHERE collection=? AND key=? AND {clause}"
        )
        conn = self._connect()
        try:
            # BEGIN IMMEDIATE + a single UPDATE...WHERE means the match check
            # and the merge happen inside one atomic SQLite step (the
            # pairing.py:788-794 claim pattern) — no read-then-write gap for a
            # second concurrent caller to slip through.
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                sql,
                (
                    json.dumps(patch, ensure_ascii=True, separators=(",", ":")),
                    datetime.now(UTC).isoformat(),
                    collection,
                    key,
                    *where_params,
                ),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Row encode/decode helpers
# ---------------------------------------------------------------------------


def _columns_for(table: str) -> tuple[str, ...]:
    try:
        return _TABLE_COLUMNS[table]
    except KeyError as exc:
        raise ValueError(f"unknown table: {table!r}") from exc


def _row_tuple(columns: tuple[str, ...], key: str, row: dict[str, Any]) -> tuple[Any, ...]:
    values: list[Any] = []
    for col in columns:
        raw = key if col == "record_id" else row.get(col)
        values.append(_encode(col, raw))
    return tuple(values)


def _encode(col: str, value: Any) -> Any:
    if value is None:
        return None
    if col in _JSON_COLUMNS:
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    if col in _BOOL_COLUMNS:
        return 1 if value else 0
    return value


def _decode_row(columns: tuple[str, ...], row: sqlite3.Row) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in columns:
        value = row[col]
        if value is not None and col in _JSON_COLUMNS:
            value = json.loads(value)
        elif col in _BOOL_COLUMNS:
            value = bool(value)
        out[col] = value
    return out


def _decode_mutable_row(row: sqlite3.Row) -> dict[str, Any]:
    """Merge a ``mutable_records`` row's JSON ``value`` with its ``updated_at``."""
    value: dict[str, Any] = json.loads(row["value"]) if row["value"] else {}
    value["updated_at"] = row["updated_at"]
    return value


def _json_where(where: dict[str, Any]) -> tuple[str, list[Any]]:
    """Build a ``json_extract(value, ?) <op> ?`` clause per key (bound params only).

    ``IS`` (not ``=``) for a None value — SQL ``= NULL`` is never true, but a
    JSON ``null`` decodes to SQL ``NULL`` and callers filter on it (e.g. an
    unclaimed owner). The JSON path travels as a bound parameter, so an
    arbitrary key name is never spliced into SQL text.
    """
    clauses: list[str] = []
    params: list[Any] = []
    for field, val in where.items():
        op = "IS" if val is None else "="
        clauses.append(f"json_extract(value, ?) {op} ?")
        params.append(f"$.{field}")
        params.append(val)
    return " AND ".join(clauses), params


def _require_column(table: str, columns: tuple[str, ...], col: str) -> None:
    if col not in columns:
        raise ValueError(f"unknown column {col!r} for table {table!r}")


def _safe_order_by(table: str, columns: tuple[str, ...], order_by: str) -> str:
    parts = order_by.split()
    column = parts[0]
    _require_column(table, columns, column)
    direction = "ASC"
    if len(parts) > 1 and parts[1].upper() == "DESC":
        direction = "DESC"
    return f"{column} {direction}"
