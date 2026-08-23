"""Async PostgreSQL ArcStore backend for local PostgreSQL and Supabase."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from arctrust.audit import AuditEvent, emit

from arcstore.backends.base import STORE_TABLES
from arcstore.config import ArcStoreConfig, PostgresSettings
from arcstore.migrations import migrate
from arcstore.mutation_fence import (
    RUNNER_LEASE_COLLECTION,
    RUNNER_LEASE_KEY,
    MutationFenceRejectedError,
    RunnerFence,
)
from arcstore.source_sync import SourceSyncBackend

_logger = logging.getLogger("arcstore.backends.postgres")
_ORDER_BY = frozenset({"ts", "ts ASC", "ts DESC"})


class _SharedPool:
    """One migrated asyncpg pool and the number of backends holding it."""

    def __init__(self, pool: Any) -> None:
        self.pool = pool
        self.holders = 0


# One pool per DSN per process. Every store in every agent module constructs
# its own PostgresBackend; without sharing, a six-agent node opens dozens of
# pools and exhausts PostgreSQL's connection slots — a self-inflicted
# resource-exhaustion outage (ASI08/LLM10). The registry is connection
# infrastructure, not application state: backends keep their independent
# start/stop contract, and the last stop() for a DSN closes its pool.
_SHARED_POOLS: dict[str, _SharedPool] = {}
_SHARED_POOLS_LOOP: int | None = None
_SHARED_POOLS_LOCK: asyncio.Lock | None = None


def _pools_lock() -> asyncio.Lock:
    """The registry lock for the running event loop.

    Locks and pools both bind to the loop that created them, and one process
    can run several loops over its lifetime (each CLI ``asyncio.run``, test
    harnesses). A registry built under a dead loop is unusable, so a new loop
    starts from an empty registry; the abandoned connections close when the
    process exits or the server reaps them.
    """
    global _SHARED_POOLS_LOOP, _SHARED_POOLS_LOCK
    loop_id = id(asyncio.get_running_loop())
    if _SHARED_POOLS_LOOP != loop_id or _SHARED_POOLS_LOCK is None:
        _SHARED_POOLS.clear()
        _SHARED_POOLS_LOOP = loop_id
        _SHARED_POOLS_LOCK = asyncio.Lock()
    return _SHARED_POOLS_LOCK


class PostgresBackend(SourceSyncBackend):
    """The one production ArcStore backend; all state lives in PostgreSQL."""

    def __init__(self, settings: ArcStoreConfig | PostgresSettings) -> None:
        self._settings = (
            settings.postgres_settings() if isinstance(settings, ArcStoreConfig) else settings
        )
        self._pool: Any | None = None
        self._shared: _SharedPool | None = None

    async def start(self) -> None:
        if self._pool is not None:
            return
        dsn = self._settings.dsn.get_secret_value()
        async with _pools_lock():
            shared = _SHARED_POOLS.get(dsn)
            if shared is None:
                shared = _SharedPool(await self._create_migrated_pool(dsn))
                _SHARED_POOLS[dsn] = shared
            shared.holders += 1
            self._shared = shared
            self._pool = shared.pool

    async def _create_migrated_pool(self, dsn: str) -> Any:
        try:
            import asyncpg
        except ImportError as exc:
            raise RuntimeError("ArcStore requires the asyncpg PostgreSQL driver") from exc
        pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=self._settings.pool_min_size,
            max_size=self._settings.pool_max_size,
            command_timeout=self._settings.command_timeout,
            timeout=self._settings.connect_timeout,
            statement_cache_size=self._settings.statement_cache_size,
            ssl=self._settings.ssl_mode not in {"disable", "prefer"},
        )
        try:
            async with pool.acquire() as connection:
                async with connection.transaction():
                    await migrate(connection)
        except Exception:
            await pool.close()
            raise
        return pool

    async def stop(self) -> None:
        if self._pool is None:
            return
        dsn = self._settings.dsn.get_secret_value()
        async with _pools_lock():
            self._pool = None
            shared, self._shared = self._shared, None
            if shared is None:  # pragma: no cover - start() always pairs the handle
                return
            shared.holders -= 1
            if shared.holders <= 0 and _SHARED_POOLS.get(dsn) is shared:
                del _SHARED_POOLS[dsn]
                await shared.pool.close()

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
            params.append(_timestamp(ts_gte))
            clauses.append(f"ts >= ${len(params)}")
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

    async def source_sync_get_state(self, agent_did: str, source_id: str) -> dict[str, Any]:
        async with self._require_pool().acquire() as connection:
            row = await connection.fetchrow(
                "SELECT agent_did, source_id, cursor, status, pages, bytes_processed, fencing_token, error_code "  # noqa: E501
                "FROM connected_source_sync WHERE agent_did=$1 AND source_id=$2",
                agent_did,
                source_id,
            )
        if row is None:
            return {
                "agent_did": agent_did,
                "source_id": source_id,
                "status": "idle",
                "pages": 0,
                "bytes_processed": 0,
                "fencing_token": 0,
            }
        return dict(row)

    async def source_sync_acquire_lease(
        self, agent_did: str, source_id: str, owner_id: str, ttl_seconds: float
    ) -> dict[str, Any] | None:
        if ttl_seconds <= 0:
            raise ValueError("lease ttl must be positive")
        async with self._require_pool().acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    "INSERT INTO connected_source_sync(agent_did, source_id, status, lease_owner, lease_expires_at, fencing_token) "  # noqa: E501
                    "VALUES ($1, $2, 'running', $3, now() + ($4 * interval '1 second'), 1) "
                    "ON CONFLICT(agent_did, source_id) DO UPDATE SET status='running', lease_owner=$3, "  # noqa: E501
                    "lease_expires_at=now() + ($4 * interval '1 second'), fencing_token=connected_source_sync.fencing_token + 1, updated_at=now() "  # noqa: E501
                    "WHERE connected_source_sync.lease_expires_at IS NULL OR connected_source_sync.lease_expires_at <= now() OR connected_source_sync.lease_owner=$3 "  # noqa: E501
                    "RETURNING lease_owner, fencing_token, lease_expires_at",
                    agent_did,
                    source_id,
                    owner_id,
                    ttl_seconds,
                )
        return (
            None
            if row is None
            else {
                "owner_id": row["lease_owner"],
                "fencing_token": row["fencing_token"],
                "expires_at": row["lease_expires_at"],
            }
        )

    async def source_sync_commit_page(self, agent_did: str, source_id: str, **kwargs: Any) -> bool:
        async with self._require_pool().acquire() as connection:
            async with connection.transaction():
                state = await connection.fetchrow(
                    "SELECT cursor, lease_owner, fencing_token, lease_expires_at FROM connected_source_sync WHERE agent_did=$1 AND source_id=$2 FOR UPDATE",  # noqa: E501
                    agent_did,
                    source_id,
                )
                if (
                    state is None
                    or state["lease_owner"] != kwargs["owner_id"]
                    or state["fencing_token"] != kwargs["fencing_token"]
                    or state["lease_expires_at"] <= datetime.now(UTC)
                ):
                    return False
                if state["cursor"] != kwargs["expected_cursor"]:
                    return bool(
                        await connection.fetchval(
                            "SELECT EXISTS(SELECT 1 FROM connected_source_pages WHERE agent_did=$1 AND source_id=$2 AND page_id=$3)",  # noqa: E501
                            agent_did,
                            source_id,
                            kwargs["page_id"],
                        )
                    )
                inserted = await connection.fetchval(
                    "INSERT INTO connected_source_pages(agent_did, source_id, page_id, cursor_after, page_count, page_bytes) VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING RETURNING page_id",  # noqa: E501
                    agent_did,
                    source_id,
                    kwargs["page_id"],
                    kwargs["next_cursor"],
                    kwargs["page_count"],
                    kwargs["page_bytes"],
                )
                if inserted is None:
                    return True
                await connection.execute(
                    "UPDATE connected_source_sync SET cursor=$3, pages=pages+$4, bytes_processed=bytes_processed+$5, updated_at=now() WHERE agent_did=$1 AND source_id=$2",  # noqa: E501
                    agent_did,
                    source_id,
                    kwargs["next_cursor"],
                    kwargs["page_count"],
                    kwargs["page_bytes"],
                )
                return True

    async def source_sync_set_status(
        self, agent_did: str, source_id: str, status: str, **kwargs: Any
    ) -> bool:
        result = await self._require_pool().execute(
            "UPDATE connected_source_sync SET status=$3, error_code=$4, updated_at=now() WHERE agent_did=$1 AND source_id=$2 AND lease_owner=$5 AND fencing_token=$6 AND lease_expires_at > now()",  # noqa: E501
            agent_did,
            source_id,
            status,
            kwargs.get("error_code"),
            kwargs["owner_id"],
            kwargs["fencing_token"],
        )
        return str(result) == "UPDATE 1"

    async def source_sync_release_lease(
        self, agent_did: str, source_id: str, **kwargs: Any
    ) -> None:
        await self._require_pool().execute(
            "UPDATE connected_source_sync SET lease_owner=NULL, lease_expires_at=NULL, updated_at=now() WHERE agent_did=$1 AND source_id=$2 AND lease_owner=$3 AND fencing_token=$4",  # noqa: E501
            agent_did,
            source_id,
            kwargs["owner_id"],
            kwargs["fencing_token"],
        )

    async def source_sync_renew_lease(self, agent_did: str, source_id: str, **kwargs: Any) -> bool:
        result = await self._require_pool().execute(
            "UPDATE connected_source_sync SET lease_expires_at=now() + ($5 * interval '1 second'), updated_at=now() "  # noqa: E501
            "WHERE agent_did=$1 AND source_id=$2 AND lease_owner=$3 AND fencing_token=$4 AND lease_expires_at > now()",  # noqa: E501
            agent_did,
            source_id,
            kwargs["owner_id"],
            kwargs["fencing_token"],
            kwargs["ttl_seconds"],
        )
        return str(result) == "UPDATE 1"

    async def mutable_write(
        self,
        collection: str,
        key: str,
        value: dict[str, Any],
        *,
        actor_did: str,
        sink: Any | None = None,
        fence: RunnerFence | None = None,
    ) -> None:
        async with self._require_pool().acquire() as connection:
            if fence is None:
                await connection.execute(
                    "INSERT INTO mutable_records(collection, key, value) "
                    "VALUES ($1, $2, $3::jsonb) "
                    "ON CONFLICT(collection, key) DO UPDATE SET "
                    "value=EXCLUDED.value, updated_at=now()",
                    collection,
                    key,
                    _json(value),
                )
            else:
                async with connection.transaction():
                    await self._assert_fence_current(connection, fence)
                    await connection.execute(
                        "INSERT INTO mutable_records(collection, key, value) "
                        "VALUES ($1, $2, $3::jsonb) "
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
            statement += " AND " + " AND ".join(_where_clauses(where, params))
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
        fence: RunnerFence | None = None,
    ) -> bool:
        async with self._require_pool().acquire() as connection:
            if fence is None:
                result = await connection.execute(
                    "UPDATE mutable_records SET value=value || $1::jsonb, updated_at=now() "
                    "WHERE collection=$2 AND key=$3",
                    _json(patch),
                    collection,
                    key,
                )
            else:
                async with connection.transaction():
                    await self._assert_fence_current(connection, fence)
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
        fence: RunnerFence | None = None,
    ) -> bool:
        async with self._require_pool().acquire() as connection:
            async with connection.transaction():
                if fence is not None:
                    await self._assert_fence_current(connection, fence)
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
        fence: RunnerFence | None = None,
    ) -> list[dict[str, Any]]:
        async with self._require_pool().acquire() as connection:
            async with connection.transaction():
                if fence is not None:
                    await self._assert_fence_current(connection, fence)
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
        fence: RunnerFence | None = None,
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
            if fence is None:
                result = await connection.execute(statement, *params)
            else:
                async with connection.transaction():
                    await self._assert_fence_current(connection, fence)
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
        fence: RunnerFence | None = None,
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
                fence=fence,
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
        statement = (
            f"UPDATE mutable_records SET value={expression}, updated_at=now() "  # noqa: S608
            "WHERE collection=$2 AND key=$3 AND " + " AND ".join(_where_clauses(where, params))
        )
        async with self._require_pool().acquire() as connection:
            if fence is None:
                result = await connection.execute(statement, *params)
            else:
                async with connection.transaction():
                    await self._assert_fence_current(connection, fence)
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
        fence: RunnerFence | None = None,
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
            if fence is None:
                result = await connection.execute(statement, *params)
            else:
                async with connection.transaction():
                    await self._assert_fence_current(connection, fence)
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

    async def enqueue_mail(self, event_id: str, envelope: dict[str, Any]) -> None:
        """Public typed seam for the Agent Mail outbox adapter."""
        async with self._require_pool().acquire() as connection:
            await connection.execute(
                "INSERT INTO mail_outbox(event_id, envelope) VALUES ($1, $2::jsonb) "
                "ON CONFLICT(event_id) DO NOTHING",
                event_id,
                _json(envelope),
            )

    async def claim_mail(self, consumer_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        statement = """
            WITH ready AS (
                SELECT event_id FROM mail_outbox
                WHERE (status='pending' OR (status='leased' AND lease_until <= now()))
                  AND available_at <= now()
                ORDER BY available_at, created_at FOR UPDATE SKIP LOCKED LIMIT $1
            )
            UPDATE mail_outbox AS m SET status='leased', lease_owner=$2,
                lease_until=now() + interval '60 seconds', attempts=attempts+1
            FROM ready WHERE m.event_id=ready.event_id
            RETURNING m.event_id, m.envelope, m.attempts, m.available_at
        """
        async with self._require_pool().acquire() as connection:
            async with connection.transaction():
                rows = await connection.fetch(statement, limit, consumer_id)
        return [
            {
                "event_id": row["event_id"],
                "envelope": _as_dict(row["envelope"]),
                "attempts": row["attempts"],
                "available_at": row["available_at"],
            }
            for row in rows
        ]

    async def ack_mail(self, consumer_id: str, event_id: str) -> bool:
        async with self._require_pool().acquire() as connection:
            result = await connection.execute(
                "UPDATE mail_outbox SET status='delivered', delivered_at=now(), "
                "lease_owner=NULL, lease_until=NULL WHERE event_id=$1 AND lease_owner=$2 "
                "AND status='leased'",
                event_id,
                consumer_id,
            )
        return str(result).endswith("1")

    async def nack_mail(
        self, consumer_id: str, event_id: str, *, retry_after_seconds: float
    ) -> bool:
        async with self._require_pool().acquire() as connection:
            result = await connection.execute(
                "UPDATE mail_outbox SET status='pending', available_at=now() + "
                "$1::double precision * interval '1 second', lease_owner=NULL, "
                "lease_until=NULL WHERE event_id=$2 AND lease_owner=$3 AND status='leased'",
                retry_after_seconds,
                event_id,
                consumer_id,
            )
        return str(result).endswith("1")

    async def dead_letter_mail(self, consumer_id: str, event_id: str, *, reason: str) -> bool:
        """Terminally retain a leased mail envelope after bounded retries."""
        if not reason:
            raise ValueError("dead-letter reason is required")
        async with self._require_pool().acquire() as connection:
            result = await connection.execute(
                "UPDATE mail_outbox SET status='dead_lettered', failure_reason=$1, "
                "dead_lettered_at=now(), lease_owner=NULL, lease_until=NULL "
                "WHERE event_id=$2 AND lease_owner=$3 AND status='leased'",
                reason,
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
        statement = "UPDATE mutable_records SET value=value || $1::jsonb, updated_at=now() "
        params: list[Any] = [_json(patch), collection, key]
        statement += "WHERE collection=$2 AND key=$3 AND " + " AND ".join(
            _where_clauses(where, params)
        )
        if absent_where:
            statement += (
                " AND NOT EXISTS (SELECT 1 FROM mutable_records m2 "  # noqa: S608
                "WHERE m2.collection=$2 "
                "AND m2.key<>$3 AND "
                + " AND ".join(_where_clauses(absent_where, params, value_column="m2.value"))
                + ")"
            )
        result = await connection.execute(statement, *params)
        return str(result).endswith("1")

    async def _assert_fence_current(self, connection: Any, fence: RunnerFence) -> None:
        """Lock and validate the runner lease inside the caller's transaction.

        ``FOR UPDATE`` is intentionally on the lease row, not a preflight
        read.  A replacement acquisition updates that same row, so it cannot
        slip between this check and the protected mutable-record write.
        """
        row = await connection.fetchrow(
            "SELECT 1 FROM mutable_records "
            "WHERE collection=$1 AND key=$2 "
            "AND value->>'owner_id'=$3 "
            "AND (value->>'fencing_token')::bigint=$4 "
            "AND (value->>'expires_at')::timestamptz > now() "
            "FOR UPDATE",
            RUNNER_LEASE_COLLECTION,
            RUNNER_LEASE_KEY,
            fence.owner_id,
            fence.token,
        )
        if row is None:
            raise MutationFenceRejectedError("workflow runner fence is no longer current")

    def _require_pool(self) -> Any:
        if self._pool is None:
            raise RuntimeError("PostgresBackend is not started")
        return self._pool

    @staticmethod
    def _require_table(table: str) -> None:
        if table not in STORE_TABLES:
            raise ValueError(f"unknown ArcStore table: {table!r}")


def _where_clauses(
    where: Mapping[str, Any], params: list[Any], *, value_column: str = "value"
) -> list[str]:
    """Compile FakeBackend-compatible dotted JSON equality predicates safely."""
    clauses: list[str] = []
    for path, expected in where.items():
        params.extend([path.split("."), _json(expected)])
        clauses.append(
            f"{value_column} #> ${len(params) - 1}::text[] "
            f"IS NOT DISTINCT FROM ${len(params)}::jsonb"
        )
    return clauses


def _json(value: Any) -> str:
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
