"""Async PostgreSQL ArcStore backend for local PostgreSQL and Supabase."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from arctrust.audit import AuditEvent, emit

from arcstore.backends.base import STORE_TABLES
from arcstore.config import ArcStoreConfig, PostgresSettings
from arcstore.migrations import migrate

_logger = logging.getLogger("arcstore.backends.postgres")
_ORDER_BY = frozenset({"ts", "ts ASC", "ts DESC"})


class PostgresBackend:
    """The one production ArcStore backend; all state lives in PostgreSQL."""

    def __init__(self, settings: ArcStoreConfig | PostgresSettings) -> None:
        self._settings = (
            settings.postgres_settings() if isinstance(settings, ArcStoreConfig) else settings
        )
        self._pool: Any | None = None

    async def start(self) -> None:
        if self._pool is not None:
            return
        try:
            import asyncpg
        except ImportError as exc:
            raise RuntimeError("ArcStore requires the asyncpg PostgreSQL driver") from exc
        self._pool = await asyncpg.create_pool(
            dsn=self._settings.dsn.get_secret_value(),
            min_size=self._settings.pool_min_size,
            max_size=self._settings.pool_max_size,
            command_timeout=self._settings.command_timeout,
            timeout=self._settings.connect_timeout,
            statement_cache_size=self._settings.statement_cache_size,
            ssl=self._settings.ssl_mode not in {"disable", "prefer"},
        )
        try:
            async with self._pool.acquire() as connection:
                async with connection.transaction():
                    await migrate(connection)
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def upsert(self, table: str, key: str, row: dict[str, Any]) -> None:
        await self.upsert_many(table, [(key, row)])

    async def upsert_many(self, table: str, rows: list[tuple[str, dict[str, Any]]]) -> None:
        self._require_table(table)
        if not rows:
            return
        pool = self._require_pool()
        statement = (
            f"INSERT INTO {table}(record_key, payload, ts) VALUES ($1, $2::jsonb, "  # noqa: S608
            "COALESCE($3::timestamptz, now())) ON CONFLICT (record_key) DO NOTHING"
        )
        async with pool.acquire() as connection:
            async with connection.transaction():
                for key, row in rows:
                    payload = {**row, "record_id": key}
                    await connection.execute(
                        statement, key, _json(payload), _timestamp(payload.get("ts"))
                    )

    async def query(
        self,
        table: str,
        *,
        where: dict[str, Any] | None = None,
        ts_gte: str | None = None,
        order_by: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        self._require_table(table)
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative")
        ordering = "ts ASC" if order_by == "ts" else (order_by or "ts DESC")
        if ordering not in _ORDER_BY:
            raise ValueError(f"unsupported order_by: {order_by!r}")
        clauses: list[str] = []
        params: list[Any] = []
        if ts_gte is not None:
            params.append(ts_gte)
            clauses.append(f"ts >= ${len(params)}::timestamptz")
        for field, value in (where or {}).items():
            params.extend([field, None if value is None else str(value)])
            if value is None:
                clauses.append(f"payload -> ${len(params) - 1} = 'null'::jsonb")
            else:
                clauses.append(f"payload ->> ${len(params) - 1} = ${len(params)}")
        statement = f"SELECT payload FROM {table}"  # noqa: S608
        if clauses:
            statement += " WHERE " + " AND ".join(clauses)
        statement += f" ORDER BY {ordering}"
        if limit is not None:
            params.append(limit)
            statement += f" LIMIT ${len(params)}"
        async with self._require_pool().acquire() as connection:
            rows = await connection.fetch(statement, *params)
        return [_as_dict(row["payload"]) for row in rows]

    async def get_cursor(self, name: str) -> int:
        async with self._require_pool().acquire() as connection:
            value = await connection.fetchval(
                "SELECT value FROM arcstore_cursors WHERE name=$1", name
            )
        return 0 if value is None else int(value)

    async def set_cursor(self, name: str, value: int) -> None:
        if value < 0:
            raise ValueError("cursor value must be non-negative")
        async with self._require_pool().acquire() as connection:
            await connection.execute(
                "INSERT INTO arcstore_cursors(name, value) VALUES ($1, $2) "
                "ON CONFLICT(name) DO UPDATE SET value=EXCLUDED.value",
                name,
                value,
            )

    async def mutable_write(
        self,
        collection: str,
        key: str,
        value: dict[str, Any],
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> None:
        async with self._require_pool().acquire() as connection:
            await connection.execute(
                "INSERT INTO mutable_records(collection, key, value) VALUES ($1, $2, $3::jsonb) "
                "ON CONFLICT(collection, key) DO UPDATE SET "
                "value=EXCLUDED.value, updated_at=now()",
                collection,
                key,
                _json(value),
            )
        _emit("mutable.write", collection, key, actor_did, sink)

    async def mutable_read(self, collection: str, key: str) -> dict[str, Any] | None:
        async with self._require_pool().acquire() as connection:
            row = await connection.fetchrow(
                "SELECT value, updated_at FROM mutable_records WHERE collection=$1 AND key=$2",
                collection,
                key,
            )
        return None if row is None else _mutable_row(row)

    async def mutable_delete(
        self,
        collection: str,
        key: str,
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> bool:
        async with self._require_pool().acquire() as connection:
            result = await connection.execute(
                "DELETE FROM mutable_records WHERE collection=$1 AND key=$2", collection, key
            )
        deleted = str(result).endswith("1")
        _emit(
            "mutable.delete", collection, key, actor_did, sink, "applied" if deleted else "no-op"
        )
        return deleted

    async def mutable_query(
        self,
        collection: str,
        *,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [collection]
        statement = "SELECT value, updated_at FROM mutable_records WHERE collection=$1"
        if where:
            params.append(_json(where))
            statement += f" AND value @> ${len(params)}::jsonb"
        async with self._require_pool().acquire() as connection:
            rows = await connection.fetch(statement, *params)
        return [_mutable_row(row) for row in rows]

    async def mutable_merge(
        self,
        collection: str,
        key: str,
        patch: dict[str, Any],
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> bool:
        async with self._require_pool().acquire() as connection:
            result = await connection.execute(
                "UPDATE mutable_records SET value=value || $1::jsonb, updated_at=now() "
                "WHERE collection=$2 AND key=$3",
                _json(patch),
                collection,
                key,
            )
        merged = str(result).endswith("1")
        _emit("mutable.merge", collection, key, actor_did, sink, "applied" if merged else "no-op")
        return merged

    async def update_if(
        self,
        collection: str,
        key: str,
        patch: dict[str, Any],
        where: dict[str, Any],
        *,
        actor_did: str,
        sink: Any | None = None,
        absent_where: dict[str, Any] | None = None,
    ) -> bool:
        async with self._require_pool().acquire() as connection:
            async with connection.transaction():
                won = await self._update_if(
                    connection, collection, key, patch, where, absent_where
                )
        _emit("mutable.update_if", collection, key, actor_did, sink, "applied" if won else "no-op")
        return won

    async def mutable_create_batch(
        self,
        collection: str,
        entries: Sequence[tuple[str, dict[str, Any]]],
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> list[dict[str, Any]]:
        async with self._require_pool().acquire() as connection:
            async with connection.transaction():
                for key, value in entries:
                    await connection.execute(
                        "INSERT INTO mutable_records(collection, key, value) "
                        "VALUES ($1, $2, $3::jsonb) "
                        "ON CONFLICT(collection, key) DO NOTHING",
                        collection,
                        key,
                        _json(value),
                    )
                rows: list[dict[str, Any]] = []
                for key, _ in entries:
                    row = await connection.fetchrow(
                        "SELECT value, updated_at FROM mutable_records "
                        "WHERE collection=$1 AND key=$2",
                        collection,
                        key,
                    )
                    if row is None:
                        raise RuntimeError("ArcStore batch row disappeared before commit")
                    rows.append(_mutable_row(row))
        _emit("mutable.create_batch", collection, "*", actor_did, sink)
        return rows

    async def mutable_increment(
        self,
        collection: str,
        key: str,
        deltas: dict[str, int | float],
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> bool:
        if not deltas:
            return False
        expression = "value"
        params: list[Any] = []
        for path, delta in deltas.items():
            params.extend([path.split("."), delta])
            path_ref = len(params) - 1
            delta_ref = len(params)
            expression = (
                f"jsonb_set({expression}, ${path_ref}::text[], "
                f"to_jsonb(COALESCE(({expression} #>> ${path_ref}::text[])::numeric, 0) "
                f"+ ${delta_ref}), true)"
            )
        params.extend([collection, key])
        statement = (
            f"UPDATE mutable_records SET value={expression}, updated_at=now() "  # noqa: S608
            f"WHERE collection=${len(params) - 1} AND key=${len(params)}"
        )
        async with self._require_pool().acquire() as connection:
            result = await connection.execute(statement, *params)
        incremented = str(result).endswith("1")
        _emit(
            "mutable.increment",
            collection,
            key,
            actor_did,
            sink,
            "applied" if incremented else "no-op",
        )
        return incremented

    async def update_if_increment(
        self,
        collection: str,
        key: str,
        patch: dict[str, Any],
        deltas: dict[str, int | float],
        where: dict[str, Any],
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> bool:
        """Conditionally patch and increment one JSON row in one transaction."""
        if not deltas:
            return await self.update_if(
                collection,
                key,
                patch,
                where,
                actor_did=actor_did,
                sink=sink,
            )
        params: list[Any] = [_json(patch), collection, key]
        expression = "value || $1::jsonb"
        for path, delta in deltas.items():
            params.extend([path.split("."), delta])
            path_ref = len(params) - 1
            delta_ref = len(params)
            expression = (
                f"jsonb_set({expression}, ${path_ref}::text[], "
                f"to_jsonb(COALESCE(({expression} #>> ${path_ref}::text[])::numeric, 0) "
                f"+ ${delta_ref}), true)"
            )
        params.append(_json(where))
        where_ref = len(params)
        statement = (
            f"UPDATE mutable_records SET value={expression}, updated_at=now() "  # noqa: S608
            f"WHERE collection=$2 AND key=$3 AND value @> ${where_ref}::jsonb"
        )
        async with self._require_pool().acquire() as connection:
            result = await connection.execute(statement, *params)
        won = str(result).endswith("1")
        _emit(
            "mutable.update_if_increment",
            collection,
            key,
            actor_did,
            sink,
            "applied" if won else "no-op",
        )
        return won

    async def append_if_absent(
        self,
        collection: str,
        key: str,
        field: str,
        item: dict[str, Any],
        *,
        length_field: str | None = None,
        actor_did: str,
        sink: Any | None = None,
    ) -> bool:
        """Append one JSON object only if the array does not already contain it."""
        path = field.split(".")
        params: list[Any] = [path, _json(item), collection, key]
        appended = (
            "jsonb_set(value, $1::text[], COALESCE(value #> $1::text[], '[]'::jsonb) "
            "|| jsonb_build_array($2::jsonb), true)"
        )
        if length_field is not None:
            params.append(length_field.split("."))
            length_ref = len(params)
            appended = (
                f"jsonb_set({appended}, ${length_ref}::text[], "
                f"to_jsonb(COALESCE((value #>> ${length_ref}::text[])::int, "
                "jsonb_array_length(COALESCE(value #> $1::text[], '[]'::jsonb))) + 1), true)"
            )
        statement = (
            f"UPDATE mutable_records SET value={appended}, updated_at=now() "  # noqa: S608
            "WHERE collection=$3 AND key=$4 "
            "AND NOT COALESCE((value #> $1::text[]) @> jsonb_build_array($2::jsonb), false)"
        )
        async with self._require_pool().acquire() as connection:
            result = await connection.execute(statement, *params)
        won = str(result).endswith("1")
        _emit(
            "mutable.append_if_absent",
            collection,
            key,
            actor_did,
            sink,
            "applied" if won else "no-op",
        )
        return won

    async def update_if_with_outbox(
        self,
        collection: str,
        key: str,
        patch: dict[str, Any],
        where: dict[str, Any],
        *,
        event_id: str,
        event: dict[str, Any],
        actor_did: str,
        sink: Any | None = None,
    ) -> bool:
        async with self._require_pool().acquire() as connection:
            async with connection.transaction():
                won = await self._update_if(connection, collection, key, patch, where, None)
                if won:
                    await connection.execute(
                        "INSERT INTO approval_outbox(event_id, approval_id, payload) "
                        "VALUES ($1, $2, $3::jsonb) "
                        "ON CONFLICT(event_id) DO NOTHING",
                        event_id,
                        key,
                        _json(event),
                    )
        _emit("approval.resolve", collection, key, actor_did, sink, "applied" if won else "no-op")
        return won

    async def mutable_write_with_outbox(
        self,
        collection: str,
        key: str,
        value: dict[str, Any],
        *,
        event_id: str,
        event: dict[str, Any],
        actor_did: str,
        sink: Any | None = None,
    ) -> None:
        async with self._require_pool().acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "INSERT INTO mutable_records(collection, key, value) "
                    "VALUES ($1, $2, $3::jsonb) ON CONFLICT(collection, key) "
                    "DO UPDATE SET value=EXCLUDED.value, updated_at=now()",
                    collection,
                    key,
                    _json(value),
                )
                await connection.execute(
                    "INSERT INTO approval_outbox(event_id, approval_id, payload) "
                    "VALUES ($1, $2, $3::jsonb) "
                    "ON CONFLICT(event_id) DO NOTHING",
                    event_id,
                    key,
                    _json(event),
                )
        _emit("approval.create", collection, key, actor_did, sink)

    async def claim_outbox(self, consumer_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        if limit < 1 or limit > 1000:
            raise ValueError("outbox claim limit must be between 1 and 1000")
        statement = """
            WITH ready AS (
                SELECT event_id FROM approval_outbox
                WHERE status <> 'delivered' AND available_at <= now()
                  AND (lease_until IS NULL OR lease_until <= now())
                ORDER BY available_at, created_at
                FOR UPDATE SKIP LOCKED
                LIMIT $1
            )
            UPDATE approval_outbox o
            SET status='leased', lease_owner=$2, lease_until=now() + interval '60 seconds',
                attempts=attempts + 1
            FROM ready WHERE o.event_id=ready.event_id
            RETURNING o.event_id, o.approval_id, o.payload, o.attempts
        """
        async with self._require_pool().acquire() as connection:
            async with connection.transaction():
                rows = await connection.fetch(statement, limit, consumer_id)
        return [
            {
                "event_id": row["event_id"],
                "approval_id": row["approval_id"],
                "event": _as_dict(row["payload"]),
                "attempts": row["attempts"],
            }
            for row in rows
        ]

    async def ack_outbox(self, consumer_id: str, event_ids: Sequence[str]) -> None:
        if not event_ids:
            return
        async with self._require_pool().acquire() as connection:
            await connection.execute(
                "UPDATE approval_outbox SET status='delivered', delivered_at=now(), "
                "lease_owner=NULL, "
                "lease_until=NULL WHERE event_id = ANY($1::text[]) AND lease_owner=$2 "
                "AND status='leased'",
                list(event_ids),
                consumer_id,
            )

    async def nack_outbox(
        self,
        consumer_id: str,
        event_id: str,
        *,
        retry_after_seconds: float,
    ) -> bool:
        if retry_after_seconds < 0:
            raise ValueError("retry delay must not be negative")
        async with self._require_pool().acquire() as connection:
            result = await connection.execute(
                "UPDATE approval_outbox SET status='pending', available_at=now() "
                "+ $1::double precision "
                "* interval '1 second', lease_owner=NULL, lease_until=NULL WHERE event_id=$2 "
                "AND lease_owner=$3 AND status='leased'",
                retry_after_seconds,
                event_id,
                consumer_id,
            )
        return str(result).endswith("1")

    async def reject_outbox(self, consumer_id: str, event_id: str) -> bool:
        """Safely quarantine a malformed leased row from the ready queue."""
        async with self._require_pool().acquire() as connection:
            result = await connection.execute(
                "UPDATE approval_outbox SET status='delivered', delivered_at=now(), "
                "lease_owner=NULL, lease_until=NULL WHERE event_id=$1 "
                "AND lease_owner=$2 AND status='leased'",
                event_id,
                consumer_id,
            )
        return str(result).endswith("1")

    async def _update_if(
        self,
        connection: Any,
        collection: str,
        key: str,
        patch: dict[str, Any],
        where: dict[str, Any],
        absent_where: dict[str, Any] | None,
    ) -> bool:
        if absent_where:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                collection + ":" + _json(absent_where),
            )
        statement = (
            "UPDATE mutable_records SET value=value || $1::jsonb, updated_at=now() "
            "WHERE collection=$2 AND key=$3 AND value @> $4::jsonb"
        )
        params: list[Any] = [_json(patch), collection, key, _json(where)]
        if absent_where:
            params.append(_json(absent_where))
            statement += (
                f" AND NOT EXISTS (SELECT 1 FROM mutable_records m2 "  # noqa: S608
                f"WHERE m2.collection=$2 "
                f"AND m2.key<>$3 AND m2.value @> ${len(params)}::jsonb)"
            )
        result = await connection.execute(statement, *params)
        return str(result).endswith("1")

    def _require_pool(self) -> Any:
        if self._pool is None:
            raise RuntimeError("PostgresBackend is not started")
        return self._pool

    @staticmethod
    def _require_table(table: str) -> None:
        if table not in STORE_TABLES:
            raise ValueError(f"unknown ArcStore table: {table!r}")


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _timestamp(value: Any) -> datetime | None:
    """Reject ambiguous input before it reaches PostgreSQL's session timezone."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("ArcStore timestamp must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("ArcStore timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("ArcStore timestamp must include a timezone")
    return parsed


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise RuntimeError("ArcStore JSON payload is not an object")
        return parsed
    if isinstance(value, Mapping):
        return dict(value)
    raise RuntimeError("ArcStore JSON payload has an unsupported driver type")


def _mutable_row(row: Any) -> dict[str, Any]:
    value = _as_dict(row["value"])
    value["updated_at"] = row["updated_at"].isoformat()
    return value


def _emit(
    action: str,
    collection: str,
    key: str,
    actor_did: str,
    sink: Any | None,
    outcome: str = "applied",
) -> None:
    if sink is None:
        return
    try:
        emit(
            AuditEvent(
                actor_did=actor_did, action=action, target=f"{collection}/{key}", outcome=outcome
            ),
            sink,
        )
    except Exception:
        _logger.warning("ArcStore audit emission failed", exc_info=True)
