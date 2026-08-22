#!/usr/bin/env python3
"""One-time, removable migration from legacy ArcStore SQLite to PostgreSQL.

The script is intentionally outside runtime packages.  It accepts only known
ArcStore source tables, opens SQLite read-only, and writes the production v1/v2
schema using one transaction per bounded batch plus a durable checkpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, Self

from arcstore.inbox import Handoff, Inbox, Message, Thread

SCHEMA_HEAD = 2
PROTOCOL_VERSION = "arcstore-sqlite-postgres-v1-v2"
META_TABLE = "arcstore_one_time_migrations"
CHECKPOINT_TABLE = "arcstore_one_time_migration_checkpoints"
OPERATIONAL_TABLES = frozenset(
    {
        "llm_calls",
        "run_events",
        "agent_events",
        "tool_events",
        "spawn_events",
        "audit_chain",
        "skill_candidates",
        "skill_candidate_bodies",
    }
)
INBOX_TABLES = frozenset({"inboxes", "inbox_threads", "inbox_messages", "inbox_handoffs"})
DESTINATION_TABLES = (
    OPERATIONAL_TABLES
    | INBOX_TABLES
    | {
        "arcstore_cursors",
        "mutable_records",
        "approval_outbox",
    }
)
SOURCE_ALIASES = {"sync_state": "arcstore_cursors"}
IGNORED_SOURCE_TABLES = frozenset({"schema_version", "arcstore_schema"})
KNOWN_SOURCE_TABLES = DESTINATION_TABLES | frozenset(SOURCE_ALIASES) | IGNORED_SOURCE_TABLES
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_DSN_SECRET = re.compile(r"(?i)(postgres(?:ql)?://[^:/\s]+:)[^@/\s]+(@)")
_TIMESTAMP_FIELDS = frozenset(
    {"ts", "created_at", "updated_at", "read_at", "available_at", "lease_until", "delivered_at"}
)


class MigrationError(RuntimeError):
    """A fail-closed migration preflight, mapping, or verification failure."""


@dataclass(frozen=True)
class MappedRow:
    destination_table: str
    key: str
    values: dict[str, Any]
    digest: str


@dataclass(frozen=True)
class TablePlan:
    source_table: str
    destination_table: str
    rows: tuple[MappedRow, ...]
    source_count: int
    source_digest: str


@dataclass
class MigrationReport:
    dry_run: bool
    resume: bool
    source: dict[str, Any] = field(default_factory=dict)
    destination: dict[str, Any] = field(default_factory=lambda: {"type": "postgresql"})
    mapped_tables: list[str] = field(default_factory=list)
    skipped_tables: list[str] = field(default_factory=list)
    tables: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "dry_run": self.dry_run,
            "resume": self.resume,
            "source": self.source,
            "destination": self.destination,
            "mapped_tables": self.mapped_tables,
            "skipped_tables": self.skipped_tables,
            "tables": self.tables,
            "error": self.error,
        }


def _json_default(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"$bytes_base64": base64.b64encode(value).decode("ascii")}
    raise TypeError(f"unsupported source value type: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, default=_json_default, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )


def _semantic_production_value(value: Any, *, field: str = "") -> Any:
    """Normalize only timestamp representations before digest comparison."""
    if isinstance(value, Mapping):
        return {
            str(name): _semantic_production_value(item, field=str(name))
            for name, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_semantic_production_value(item) for item in value]
    if isinstance(value, str) and field in _TIMESTAMP_FIELDS:
        try:
            return _timestamp(value, field=field)
        except MigrationError:
            return value
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(
        canonical_json(_semantic_production_value(value)).encode("utf-8")
    ).hexdigest()


def _digest_rows(rows: Sequence[MappedRow]) -> str:
    return hashlib.sha256(
        "".join(row.digest for row in sorted(rows, key=lambda row: (row.key, row.digest))).encode()
    ).hexdigest()


def _redact(value: str) -> str:
    return _DSN_SECRET.sub(r"\1[REDACTED]\2", value)[:500]


def _timestamp(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise MigrationError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MigrationError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MigrationError(f"{field} must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def _database_timestamp(value: str | None, *, field: str) -> datetime | None:
    """Convert canonical report text into asyncpg's timezone-aware binding type."""
    if value is None:
        return None
    normalized = _timestamp(value, field=field)
    return datetime.fromisoformat(normalized)


def _json_object(value: Any, *, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise MigrationError(f"{field} is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise MigrationError(f"{field} must be a JSON object")
    return dict(value)


class SQLiteSource:
    """Read-only SQLite source with deterministic snapshots."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> Self:
        if not self.path.is_file():
            raise MigrationError(f"SQLite source does not exist: {self.path}")
        self._connection = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
        self._connection.execute("PRAGMA query_only=ON")
        result = self._connection.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            self.close()
            raise MigrationError("SQLite integrity check failed")
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise MigrationError("SQLite source is not open")
        return self._connection

    def schema(self) -> dict[str, Any]:
        tables = [
            str(row[0])
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            if not str(row[0]).startswith("sqlite_")
        ]
        return {
            "user_version": int(self.connection.execute("PRAGMA user_version").fetchone()[0]),
            "tables": tables,
        }

    def rows(self, table: str) -> list[dict[str, Any]]:
        if not _SAFE_IDENTIFIER.fullmatch(table):
            raise MigrationError(f"unsafe source table name: {table!r}")
        columns = [str(row[1]) for row in self.connection.execute(f'PRAGMA table_info("{table}")')]
        if not columns:
            raise MigrationError(f"source table has no columns: {table}")
        names = ",".join(f'"{column.replace(chr(34), chr(34) * 2)}"' for column in columns)
        try:
            cursor = self.connection.execute(
                f'SELECT {names} FROM "{table}" ORDER BY rowid'  # noqa: S608 - known SQLite table/columns
            )
        except sqlite3.OperationalError as exc:
            if "rowid" not in str(exc):
                raise
            cursor = self.connection.execute(
                f'SELECT {names} FROM "{table}"'  # noqa: S608 - known SQLite table/columns
            )
        return [dict(zip(columns, row, strict=True)) for row in cursor]


def _payload_row(raw: Mapping[str, Any], *, key: str, timestamp: str | None) -> dict[str, Any]:
    payload = (
        _json_object(raw["payload"], field="payload") if raw.get("payload") is not None else {}
    )
    if not payload:
        payload = {
            name: value
            for name, value in raw.items()
            if name not in {"record_key", "record_id", "id", "ts", "payload"}
        }
        for field in ("extra", "scores"):
            if isinstance(payload.get(field), str):
                payload[field] = _json_object(payload[field], field=field)
    payload.setdefault("record_id", key)
    if raw.get("ts") is not None and timestamp is not None:
        payload.setdefault("ts", timestamp)
    return payload


def _require_key(raw: Mapping[str, Any], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value:
        raise MigrationError(f"{field} is required")
    return value


def _model_payload(
    raw: Mapping[str, Any],
    model: type[Inbox] | type[Thread] | type[Message] | type[Handoff],
    *,
    identifiers: Mapping[str, str],
    timestamp_field: str,
) -> dict[str, Any]:
    payload = _json_object(raw.get("payload"), field="payload")
    for name, value in identifiers.items():
        if name in payload and payload[name] != value:
            raise MigrationError(f"payload {name} disagrees with source foreign-key column")
        payload[name] = value
    timestamp = raw.get(timestamp_field)
    if timestamp is not None:
        payload.setdefault(timestamp_field, _timestamp(timestamp, field=timestamp_field))
    try:
        return model.model_validate(payload).model_dump(mode="json")
    except ValueError as exc:
        raise MigrationError(f"invalid {model.__name__} payload") from exc


def _map_operational(table: str, raw: Mapping[str, Any]) -> MappedRow:
    key = (
        _require_key(raw, "record_key")
        if raw.get("record_key")
        else _require_key(raw, "record_id")
    )
    payload_value = _json_object(raw["payload"], field="payload") if raw.get("payload") else {}
    raw_timestamp = raw.get("ts") or payload_value.get("ts")
    timestamp = _timestamp(raw_timestamp, field="ts") if raw_timestamp else None
    values = {
        "record_key": key,
        "payload": _payload_row(raw, key=key, timestamp=timestamp),
        "ts": timestamp,
    }
    return MappedRow(table, key, values, digest(values))


def _map_row(source_table: str, raw: Mapping[str, Any]) -> MappedRow:
    table = SOURCE_ALIASES.get(source_table, source_table)
    if table in OPERATIONAL_TABLES:
        return _map_operational(table, raw)
    if table == "arcstore_cursors":
        name = _require_key(raw, "name") if raw.get("name") else _require_key(raw, "source")
        value = raw.get("value", raw.get("offset"))
        if not isinstance(value, int) or value < 0:
            raise MigrationError("cursor value must be a non-negative integer")
        values = {"name": name, "value": value}
        return MappedRow(table, name, values, digest(values))
    if table == "mutable_records":
        collection, key = _require_key(raw, "collection"), _require_key(raw, "key")
        values = {
            "collection": collection,
            "key": key,
            "value": _json_object(raw.get("value"), field="value"),
        }
        return MappedRow(table, f"{collection}\x00{key}", values, digest(values))
    if table == "inboxes":
        inbox_id = _require_key(raw, "inbox_id")
        payload = _model_payload(
            raw, Inbox, identifiers={"inbox_id": inbox_id}, timestamp_field="created_at"
        )
        values = {"inbox_id": inbox_id, "payload": payload, "created_at": payload["created_at"]}
        return MappedRow(table, inbox_id, values, digest(values))
    if table == "inbox_threads":
        thread_id, inbox_id = _require_key(raw, "thread_id"), _require_key(raw, "inbox_id")
        payload = _model_payload(
            raw,
            Thread,
            identifiers={"thread_id": thread_id, "inbox_id": inbox_id},
            timestamp_field="updated_at",
        )
        values = {
            "thread_id": thread_id,
            "inbox_id": inbox_id,
            "payload": payload,
            "updated_at": payload["updated_at"],
        }
        return MappedRow(table, thread_id, values, digest(values))
    if table == "inbox_messages":
        message_id, thread_id = _require_key(raw, "message_id"), _require_key(raw, "thread_id")
        payload = _model_payload(
            raw,
            Message,
            identifiers={"message_id": message_id, "thread_id": thread_id},
            timestamp_field="created_at",
        )
        values = {
            "message_id": message_id,
            "thread_id": thread_id,
            "payload": payload,
            "created_at": payload["created_at"],
        }
        return MappedRow(table, message_id, values, digest(values))
    if table == "inbox_handoffs":
        handoff_id, thread_id = _require_key(raw, "handoff_id"), _require_key(raw, "thread_id")
        payload = _model_payload(
            raw,
            Handoff,
            identifiers={"handoff_id": handoff_id, "thread_id": thread_id},
            timestamp_field="created_at",
        )
        values = {
            "handoff_id": handoff_id,
            "thread_id": thread_id,
            "payload": payload,
            "created_at": payload["created_at"],
        }
        return MappedRow(table, handoff_id, values, digest(values))
    if table == "approval_outbox":
        event_id, approval_id = _require_key(raw, "event_id"), _require_key(raw, "approval_id")
        status = raw.get("status", "pending")
        if status not in {"pending", "leased", "delivered"}:
            raise MigrationError("approval_outbox status is invalid")
        attempts = raw.get("attempts", 0)
        if not isinstance(attempts, int) or attempts < 0:
            raise MigrationError("approval_outbox attempts is invalid")
        values = {
            "event_id": event_id,
            "approval_id": approval_id,
            "payload": _json_object(raw.get("payload"), field="payload"),
            "status": status,
            "attempts": attempts,
            "available_at": _timestamp(
                raw.get("available_at") or raw.get("created_at"), field="available_at"
            ),
            "lease_owner": raw.get("lease_owner"),
            "lease_until": _timestamp(raw["lease_until"], field="lease_until")
            if raw.get("lease_until")
            else None,
            "delivered_at": _timestamp(raw["delivered_at"], field="delivered_at")
            if raw.get("delivered_at")
            else None,
            "created_at": _timestamp(raw.get("created_at"), field="created_at"),
        }
        return MappedRow(table, event_id, values, digest(values))
    raise MigrationError(f"unsupported source table: {source_table}")


def _plans(source: SQLiteSource) -> tuple[list[TablePlan], list[str]]:
    schema_tables = set(source.schema()["tables"])
    unknown = schema_tables - KNOWN_SOURCE_TABLES
    if unknown:
        raise MigrationError("unknown source tables refused: " + ", ".join(sorted(unknown)))
    ignored = sorted(schema_tables & IGNORED_SOURCE_TABLES)
    order = ["inboxes", "inbox_threads", "inbox_messages", "inbox_handoffs"]
    names = sorted(
        schema_tables - IGNORED_SOURCE_TABLES,
        key=lambda name: (order.index(name) if name in order else len(order), name),
    )
    plans: list[TablePlan] = []
    for name in names:
        raw_rows = source.rows(name)
        rows = sorted(
            (_map_row(name, raw) for raw in raw_rows), key=lambda row: (row.key, row.digest)
        )
        if len({row.key for row in rows}) != len(rows):
            raise MigrationError(f"duplicate source keys in {name}")
        plans.append(
            TablePlan(
                name, SOURCE_ALIASES.get(name, name), tuple(rows), len(raw_rows), digest(raw_rows)
            )
        )
    return plans, ignored


class Destination(Protocol):
    writes: int

    async def preflight(self) -> tuple[int, set[str], set[str]]: ...
    async def register(self, migration_id: str, source_schema: Mapping[str, Any]) -> None: ...
    async def checkpoint(self, migration_id: str, table: str) -> tuple[str, str] | None: ...
    async def write_batch(
        self, migration_id: str, table: str, rows: Sequence[MappedRow]
    ) -> None: ...
    async def verify(self, table: str, rows: Sequence[MappedRow]) -> tuple[int, str]: ...
    async def close(self) -> None: ...


class FakeDestination:
    """In-memory destination for deterministic one-time tool tests only."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, MappedRow]] = {table: {} for table in DESTINATION_TABLES}
        self.checkpoints: dict[tuple[str, str], tuple[str, str]] = {}
        self.migrations: set[str] = set()
        self.writes = 0

    async def preflight(self) -> tuple[int, set[str], set[str]]:
        return SCHEMA_HEAD, set(DESTINATION_TABLES), self.migrations

    async def register(self, migration_id: str, source_schema: Mapping[str, Any]) -> None:
        del source_schema
        self.migrations.add(migration_id)

    async def checkpoint(self, migration_id: str, table: str) -> tuple[str, str] | None:
        return self.checkpoints.get((migration_id, table))

    async def write_batch(self, migration_id: str, table: str, rows: Sequence[MappedRow]) -> None:
        for row in rows:
            existing = self.rows[table].get(row.key)
            if existing is not None and existing.digest != row.digest:
                raise MigrationError(f"destination key collision in {table}: {row.key}")
        for row in rows:
            self.rows[table].setdefault(row.key, row)
            self.writes += 1
        if rows:
            last = rows[-1]
            self.checkpoints[(migration_id, table)] = (last.key, last.digest)

    async def verify(self, table: str, rows: Sequence[MappedRow]) -> tuple[int, str]:
        actual = [self.rows[table][row.key] for row in rows if row.key in self.rows[table]]
        return len(actual), _digest_rows(actual)

    async def close(self) -> None:
        return None


class PostgresDestination:
    """asyncpg destination; its DSN comes only from an env/vault injection."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: Any | None = None
        self.writes = 0

    @classmethod
    def from_environment(cls, variable: str) -> PostgresDestination:
        dsn = os.environ.get(variable)
        if not dsn:
            raise MigrationError(f"destination environment variable is not set: {variable}")
        return cls(dsn)

    async def _started(self) -> Any:
        if self._pool is None:
            try:
                import asyncpg
            except ImportError as exc:
                raise MigrationError("asyncpg is required for PostgreSQL migration") from exc
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=1)
        return self._pool

    async def preflight(self) -> tuple[int, set[str], set[str]]:
        async with (await self._started()).acquire() as connection:
            tables = {
                row["table_name"]
                for row in await connection.fetch(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema=current_schema()"
                )
            }
            version = (
                await connection.fetchval("SELECT max(version) FROM arcstore_schema")
                if "arcstore_schema" in tables
                else None
            )
            migration_ids: set[str] = set()
            # META_TABLE is intentionally checked against every discovered table,
            # not the runtime-table subset: that was the old resume failure.
            if META_TABLE in tables:
                migration_ids = {
                    row["migration_id"]
                    for row in await connection.fetch(
                        f"SELECT migration_id FROM {META_TABLE}"  # noqa: S608 - fixed tool metadata table
                    )
                }
        return int(version or 0), tables, migration_ids

    async def register(self, migration_id: str, source_schema: Mapping[str, Any]) -> None:
        async with (await self._started()).acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    f"CREATE TABLE IF NOT EXISTS {META_TABLE} ("
                    "migration_id text PRIMARY KEY, protocol_version text NOT NULL, "
                    "source_schema jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now())"
                )
                await connection.execute(
                    f"CREATE TABLE IF NOT EXISTS {CHECKPOINT_TABLE} ("
                    f"migration_id text NOT NULL REFERENCES {META_TABLE}(migration_id) "
                    "ON DELETE CASCADE, source_table text NOT NULL, last_key text NOT NULL, "
                    "last_digest text NOT NULL, PRIMARY KEY(migration_id, source_table))"
                )
                await connection.execute(
                    f"INSERT INTO {META_TABLE}(migration_id, protocol_version, source_schema) "
                    "VALUES($1,$2,$3::jsonb) ON CONFLICT(migration_id) DO NOTHING",
                    migration_id,
                    PROTOCOL_VERSION,
                    canonical_json(source_schema),
                )

    async def checkpoint(self, migration_id: str, table: str) -> tuple[str, str] | None:
        async with (await self._started()).acquire() as connection:
            row = await connection.fetchrow(
                f"SELECT last_key,last_digest FROM {CHECKPOINT_TABLE} "  # noqa: S608 - fixed checkpoint table
                "WHERE migration_id=$1 AND source_table=$2",
                migration_id,
                table,
            )
        return None if row is None else (str(row["last_key"]), str(row["last_digest"]))

    async def write_batch(self, migration_id: str, table: str, rows: Sequence[MappedRow]) -> None:
        if not rows:
            return
        async with (await self._started()).acquire() as connection:
            async with connection.transaction():
                for row in rows:
                    await self._write_row(connection, row)
                    self.writes += 1
                last = rows[-1]
                await connection.execute(
                    f"INSERT INTO {CHECKPOINT_TABLE}("
                    "migration_id,source_table,last_key,last_digest) "
                    "VALUES($1,$2,$3,$4) ON CONFLICT(migration_id,source_table) "
                    "DO UPDATE SET last_key=EXCLUDED.last_key,last_digest=EXCLUDED.last_digest",
                    migration_id,
                    table,
                    last.key,
                    last.digest,
                )

    async def _write_row(self, connection: Any, row: MappedRow) -> None:
        table, values = row.destination_table, row.values
        if table in OPERATIONAL_TABLES:
            await connection.execute(
                f"INSERT INTO {table}(record_key,payload,ts) "
                "VALUES($1,$2::jsonb,COALESCE($3::timestamptz,now())) "
                "ON CONFLICT(record_key) DO NOTHING",
                values["record_key"],
                canonical_json(values["payload"]),
                _database_timestamp(values["ts"], field="ts"),
            )
        elif table == "arcstore_cursors":
            await connection.execute(
                "INSERT INTO arcstore_cursors(name,value) VALUES($1,$2) "
                "ON CONFLICT(name) DO NOTHING",
                values["name"],
                values["value"],
            )
        elif table == "mutable_records":
            await connection.execute(
                "INSERT INTO mutable_records(collection,key,value) "
                "VALUES($1,$2,$3::jsonb) ON CONFLICT(collection,key) DO NOTHING",
                values["collection"],
                values["key"],
                canonical_json(values["value"]),
            )
        elif table == "inboxes":
            await connection.execute(
                "INSERT INTO inboxes(inbox_id,payload,created_at) "
                "VALUES($1,$2::jsonb,$3::timestamptz) ON CONFLICT(inbox_id) DO NOTHING",
                values["inbox_id"],
                canonical_json(values["payload"]),
                _database_timestamp(values["created_at"], field="created_at"),
            )
        elif table == "inbox_threads":
            await connection.execute(
                "INSERT INTO inbox_threads(thread_id,inbox_id,payload,updated_at) "
                "VALUES($1,$2,$3::jsonb,$4::timestamptz) ON CONFLICT(thread_id) DO NOTHING",
                values["thread_id"],
                values["inbox_id"],
                canonical_json(values["payload"]),
                _database_timestamp(values["updated_at"], field="updated_at"),
            )
        elif table == "inbox_messages":
            await connection.execute(
                "INSERT INTO inbox_messages(message_id,thread_id,payload,created_at) "
                "VALUES($1,$2,$3::jsonb,$4::timestamptz) ON CONFLICT(message_id) DO NOTHING",
                values["message_id"],
                values["thread_id"],
                canonical_json(values["payload"]),
                _database_timestamp(values["created_at"], field="created_at"),
            )
        elif table == "inbox_handoffs":
            await connection.execute(
                "INSERT INTO inbox_handoffs(handoff_id,thread_id,payload,created_at) "
                "VALUES($1,$2,$3::jsonb,$4::timestamptz) ON CONFLICT(handoff_id) DO NOTHING",
                values["handoff_id"],
                values["thread_id"],
                canonical_json(values["payload"]),
                _database_timestamp(values["created_at"], field="created_at"),
            )
        elif table == "approval_outbox":
            await connection.execute(
                "INSERT INTO approval_outbox("
                "event_id,approval_id,payload,status,attempts,available_at,lease_owner,"
                "lease_until,delivered_at,created_at) "
                "VALUES($1,$2,$3::jsonb,$4,$5,$6::timestamptz,$7,$8::timestamptz,"
                "$9::timestamptz,$10::timestamptz) ON CONFLICT(event_id) DO NOTHING",
                values["event_id"],
                values["approval_id"],
                canonical_json(values["payload"]),
                values["status"],
                values["attempts"],
                _database_timestamp(values["available_at"], field="available_at"),
                values["lease_owner"],
                _database_timestamp(values["lease_until"], field="lease_until"),
                _database_timestamp(values["delivered_at"], field="delivered_at"),
                _database_timestamp(values["created_at"], field="created_at"),
            )
        else:
            raise MigrationError(f"unsupported destination table: {table}")

    async def verify(self, table: str, rows: Sequence[MappedRow]) -> tuple[int, str]:
        async with (await self._started()).acquire() as connection:
            actual = [await self._read_row(connection, table, row.key) for row in rows]
        present = [row for row in actual if row is not None]
        return len(present), _digest_rows(present)

    async def _read_row(self, connection: Any, table: str, key: str) -> MappedRow | None:
        if table in OPERATIONAL_TABLES:
            row = await connection.fetchrow(
                f"SELECT record_key,payload::text,ts FROM {table} WHERE record_key=$1",  # noqa: S608 - closed operational allowlist
                key,
            )
            values = (
                None
                if row is None
                else {
                    "record_key": row["record_key"],
                    "payload": json.loads(row["payload"]),
                    "ts": None
                    if table == "skill_candidate_bodies"
                    else row["ts"].astimezone(UTC).isoformat(),
                }
            )
        elif table == "arcstore_cursors":
            row = await connection.fetchrow(
                "SELECT name,value FROM arcstore_cursors WHERE name=$1", key
            )
            values = None if row is None else {"name": row["name"], "value": row["value"]}
        elif table == "mutable_records":
            collection, record_key = key.split("\x00", 1)
            row = await connection.fetchrow(
                "SELECT collection,key,value::text FROM mutable_records "
                "WHERE collection=$1 AND key=$2",
                collection,
                record_key,
            )
            values = (
                None
                if row is None
                else {
                    "collection": row["collection"],
                    "key": row["key"],
                    "value": json.loads(row["value"]),
                }
            )
        elif table in INBOX_TABLES:
            id_column = {
                "inboxes": "inbox_id",
                "inbox_threads": "thread_id",
                "inbox_messages": "message_id",
                "inbox_handoffs": "handoff_id",
            }[table]
            columns = {
                "inboxes": "inbox_id,payload::text,created_at",
                "inbox_threads": "thread_id,inbox_id,payload::text,updated_at",
                "inbox_messages": "message_id,thread_id,payload::text,created_at",
                "inbox_handoffs": "handoff_id,thread_id,payload::text,created_at",
            }[table]
            row = await connection.fetchrow(
                f"SELECT {columns} FROM {table} WHERE {id_column}=$1",  # noqa: S608 - closed inbox allowlist
                key,
            )
            if row is None:
                values = None
            else:
                timestamp = "updated_at" if table == "inbox_threads" else "created_at"
                values = {
                    id_column: row[id_column],
                    "payload": json.loads(row["payload"]),
                    timestamp: row[timestamp].astimezone(UTC).isoformat(),
                }
                if table != "inboxes":
                    values["inbox_id" if table == "inbox_threads" else "thread_id"] = row[
                        "inbox_id" if table == "inbox_threads" else "thread_id"
                    ]
        else:
            row = await connection.fetchrow(
                "SELECT event_id,approval_id,payload::text,status,attempts,available_at,"
                "lease_owner,lease_until,delivered_at,created_at FROM approval_outbox "
                "WHERE event_id=$1",
                key,
            )
            values = (
                None
                if row is None
                else {
                    name: (
                        row[name].astimezone(UTC).isoformat()
                        if name in {"available_at", "lease_until", "delivered_at", "created_at"}
                        and row[name] is not None
                        else json.loads(row[name])
                        if name == "payload"
                        else row[name]
                    )
                    for name in (
                        "event_id",
                        "approval_id",
                        "payload",
                        "status",
                        "attempts",
                        "available_at",
                        "lease_owner",
                        "lease_until",
                        "delivered_at",
                        "created_at",
                    )
                }
            )
        return None if values is None else MappedRow(table, key, values, digest(values))

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


async def migrate(
    source_path: Path,
    destination: Destination,
    *,
    batch_size: int = 500,
    dry_run: bool = False,
    resume: bool = False,
    backup_path: Path | None = None,
) -> MigrationReport:
    if not 1 <= batch_size <= 10_000:
        raise ValueError("batch_size must be between 1 and 10000")
    report = MigrationReport(dry_run=dry_run, resume=resume)
    source_path = source_path.expanduser().resolve()
    if backup_path is not None:
        backup = backup_path.expanduser().resolve()
        if backup == source_path:
            raise MigrationError("backup path must differ from source")
        backup.parent.mkdir(parents=True, exist_ok=True)
        with (
            sqlite3.connect(
                f"file:{source_path.as_posix()}?mode=ro", uri=True
            ) as source_connection,
            sqlite3.connect(backup) as backup_connection,
        ):
            source_connection.backup(backup_connection)
        report.source["backup_path"] = str(backup)
    with SQLiteSource(source_path) as source:
        schema = source.schema()
        plans, skipped = _plans(source)
        migration_id = digest(
            {
                "protocol": PROTOCOL_VERSION,
                "schema": schema,
                "plans": [(plan.source_table, _digest_rows(plan.rows)) for plan in plans],
            }
        )
        report.source.update({"path": str(source.path), "read_only": True, "schema": schema})
        report.destination["migration_id"] = migration_id
        report.skipped_tables = skipped
        version, present_tables, migrations = await destination.preflight()
        if version != SCHEMA_HEAD:
            raise MigrationError(f"destination schema version must be {SCHEMA_HEAD}")
        missing = sorted(DESTINATION_TABLES - present_tables)
        if missing:
            raise MigrationError("destination missing production tables: " + ", ".join(missing))
        if migration_id in migrations and not resume:
            raise MigrationError("migration already registered; pass --resume")
        if not dry_run:
            await destination.register(migration_id, schema)
        for plan in plans:
            source_digest = _digest_rows(plan.rows)
            info: dict[str, Any] = {
                "destination_table": plan.destination_table,
                "source_count": plan.source_count,
                "source_digest": source_digest,
                "migrated_count": 0,
                "batches": 0,
            }
            report.mapped_tables.append(plan.source_table)
            if dry_run:
                info.update(
                    {
                        "migrated_count": len(plan.rows),
                        "batches": (len(plan.rows) + batch_size - 1) // batch_size,
                        "destination_count": None,
                        "destination_digest": None,
                    }
                )
                report.tables[plan.source_table] = info
                continue
            checkpoint = await destination.checkpoint(migration_id, plan.destination_table)
            start = 0
            if checkpoint is not None:
                try:
                    start = next(
                        index + 1
                        for index, row in enumerate(plan.rows)
                        if (row.key, row.digest) == checkpoint
                    )
                except StopIteration as exc:
                    raise MigrationError(f"stale checkpoint for {plan.source_table}") from exc
            for offset in range(start, len(plan.rows), batch_size):
                batch = plan.rows[offset : offset + batch_size]
                await destination.write_batch(migration_id, plan.destination_table, batch)
                info["migrated_count"] += len(batch)
                info["batches"] += 1
            count, destination_digest = await destination.verify(plan.destination_table, plan.rows)
            info.update({"destination_count": count, "destination_digest": destination_digest})
            if count != len(plan.rows) or destination_digest != source_digest:
                raise MigrationError(
                    f"count or digest verification failed for {plan.destination_table}"
                )
            report.tables[plan.source_table] = info
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", required=True, type=Path)
    parser.add_argument(
        "--postgres-env",
        default="ARCSTORE_DATABASE_URL",
        help="environment variable containing a vault-injected PostgreSQL DSN",
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--report", type=Path)
    return parser


async def _main_async(args: argparse.Namespace) -> MigrationReport:
    destination = PostgresDestination.from_environment(args.postgres_env)
    try:
        return await migrate(
            args.sqlite,
            destination,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            resume=args.resume,
            backup_path=args.backup,
        )
    finally:
        await destination.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = MigrationReport(dry_run=args.dry_run, resume=args.resume)
    try:
        report = asyncio.run(_main_async(args))
    except Exception as exc:
        report.error = _redact(str(exc))
    output = canonical_json(report.as_dict()) + "\n"
    if args.report is not None:
        args.report.write_text(output, encoding="utf-8")
    sys.stdout.write(output)
    return 1 if report.error else 0


if __name__ == "__main__":
    raise SystemExit(main())
