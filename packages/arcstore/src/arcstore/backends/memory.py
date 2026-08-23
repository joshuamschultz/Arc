"""In-memory implementation of the complete ArcStore backend contract."""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from arctrust.audit import AuditEvent, emit

from arcstore.backends.base import APPROVAL_OUTBOX_TABLE, MAIL_OUTBOX_TABLE
from arcstore.mutation_fence import (
    RUNNER_LEASE_COLLECTION,
    RUNNER_LEASE_KEY,
    MutationFenceRejectedError,
    RunnerFence,
)
from arcstore.source_sync import SourceSyncBackend


def _now() -> str:
    return datetime.now(UTC).isoformat()


_MISSING = object()


def _lookup(value: dict[str, Any], path: str) -> Any:
    """Resolve the dotted JSON path syntax shared by store backends."""
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _matches(value: dict[str, Any], where: dict[str, Any]) -> bool:
    return all(_lookup(value, field) == expected for field, expected in where.items())


def _increment(value: dict[str, Any], path: str, delta: int | float) -> None:
    current = value
    parts = path.split(".")
    for part in parts[:-1]:
        nested = current.get(part)
        if not isinstance(nested, dict):
            nested = {}
            current[part] = nested
        current = nested
    leaf = parts[-1]
    previous = current.get(leaf, 0)
    if not isinstance(previous, (int, float)):
        raise ValueError(f"mutable value at {path!r} is not numeric")
    current[leaf] = previous + delta


class FakeBackend(SourceSyncBackend):
    """Lock-protected contract fake used to test every backend-neutral domain."""

    def __init__(self) -> None:
        self._tables: dict[str, dict[str, dict[str, Any]]] = {}
        self._cursors: dict[str, int] = {}
        self._mutable: dict[tuple[str, str], tuple[dict[str, Any], str]] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def _assert_fence_locked(self, fence: RunnerFence | None) -> None:
        """Validate an ArcFlow fence while the mutation lock is held."""
        if fence is None:
            return
        lease = self._mutable.get((RUNNER_LEASE_COLLECTION, RUNNER_LEASE_KEY))
        if lease is None:
            raise MutationFenceRejectedError("workflow runner lease is absent")
        value = lease[0]
        try:
            expires_at = datetime.fromisoformat(str(value["expires_at"]).replace("Z", "+00:00"))
            current = (
                value.get("owner_id") == fence.owner_id
                and int(value.get("fencing_token", -1)) == fence.token
                and expires_at > datetime.now(UTC)
            )
        except (KeyError, TypeError, ValueError):
            current = False
        if not current:
            raise MutationFenceRejectedError("workflow runner fence is no longer current")

    async def upsert(self, table: str, key: str, row: dict[str, Any]) -> None:
        await self.upsert_many(table, [(key, row)])

    async def upsert_many(self, table: str, rows: list[tuple[str, dict[str, Any]]]) -> None:
        async with self._lock:
            bucket = self._tables.setdefault(table, {})
            for key, row in rows:
                bucket.setdefault(key, {**copy.deepcopy(row), "record_id": key})

    async def query(
        self,
        table: str,
        *,
        where: dict[str, Any] | None = None,
        ts_gte: str | None = None,
        order_by: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative")
        async with self._lock:
            rows = [copy.deepcopy(row) for row in self._tables.get(table, {}).values()]
        if where:
            rows = [row for row in rows if _matches(row, where)]
        if ts_gte is not None:
            rows = [row for row in rows if (row.get("ts") or "") >= ts_gte]
        if order_by:
            column, reverse = _parse_order_by(order_by)
            rows.sort(key=lambda row: (row.get(column) is None, row.get(column)), reverse=reverse)
        return rows if limit is None else rows[:limit]

    async def get_cursor(self, name: str) -> int:
        async with self._lock:
            return self._cursors.get(name, 0)

    async def set_cursor(self, name: str, value: int) -> None:
        if value < 0:
            raise ValueError("cursor value must be non-negative")
        async with self._lock:
            self._cursors[name] = value

    async def source_sync_get_state(self, agent_did: str, source_id: str) -> dict[str, Any]:
        source_key = f"{agent_did}\0{source_id}"
        async with self._lock:
            row = self._tables.setdefault("connected_source_sync", {}).get(source_key)
            return copy.deepcopy(
                row
                or {
                    "agent_did": agent_did,
                    "source_id": source_id,
                    "status": "idle",
                    "pages": 0,
                    "bytes_processed": 0,
                    "fencing_token": 0,
                }
            )

    async def source_sync_acquire_lease(
        self, agent_did: str, source_id: str, owner_id: str, ttl_seconds: float
    ) -> dict[str, Any] | None:
        source_key = f"{agent_did}\0{source_id}"
        if ttl_seconds <= 0:
            raise ValueError("lease ttl must be positive")
        now = datetime.now(UTC)
        async with self._lock:
            table = self._tables.setdefault("connected_source_sync", {})
            row = table.setdefault(
                source_key,
                {
                    "agent_did": agent_did,
                    "source_id": source_id,
                    "status": "idle",
                    "pages": 0,
                    "bytes_processed": 0,
                    "fencing_token": 0,
                },
            )
            expires = row.get("lease_expires_at")
            if (
                expires is not None
                and datetime.fromisoformat(expires) > now
                and row.get("lease_owner") != owner_id
            ):
                return None
            token = int(row.get("fencing_token", 0)) + 1
            expires_at = now + timedelta(seconds=ttl_seconds)
            row.update(
                {
                    "fencing_token": token,
                    "lease_owner": owner_id,
                    "lease_expires_at": expires_at.isoformat(),
                    "status": "running",
                }
            )
            return {
                "owner_id": owner_id,
                "fencing_token": token,
                "expires_at": expires_at.isoformat(),
            }

    async def source_sync_commit_page(self, agent_did: str, source_id: str, **kwargs: Any) -> bool:
        source_key = f"{agent_did}\0{source_id}"
        async with self._lock:
            table = self._tables.setdefault("connected_source_sync", {})
            row = table.setdefault(
                source_key,
                {
                    "agent_did": agent_did,
                    "source_id": source_id,
                    "status": "idle",
                    "pages": 0,
                    "bytes_processed": 0,
                    "fencing_token": 0,
                },
            )
            expires = row.get("lease_expires_at")
            if (
                row.get("lease_owner") != kwargs["owner_id"]
                or int(row.get("fencing_token", -1)) != kwargs["fencing_token"]
                or expires is None
                or datetime.fromisoformat(expires) <= datetime.now(UTC)
            ):
                return False
            pages = self._tables.setdefault("connected_source_pages", {})
            key = f"{source_key}\0{kwargs['page_id']}"
            if row.get("cursor") != kwargs["expected_cursor"]:
                return key in pages
            if key in pages:
                return True
            pages[key] = {
                "cursor_after": kwargs["next_cursor"],
                "page_count": kwargs["page_count"],
                "page_bytes": kwargs["page_bytes"],
            }
            row.update(
                {
                    "cursor": kwargs["next_cursor"],
                    "pages": int(row.get("pages", 0)) + kwargs["page_count"],
                    "bytes_processed": int(row.get("bytes_processed", 0)) + kwargs["page_bytes"],
                }
            )
            return True

    async def source_sync_set_status(
        self, agent_did: str, source_id: str, status: str, **kwargs: Any
    ) -> bool:
        source_key = f"{agent_did}\0{source_id}"
        async with self._lock:
            row = self._tables.setdefault("connected_source_sync", {}).setdefault(
                source_key,
                {
                    "agent_did": agent_did,
                    "source_id": source_id,
                    "pages": 0,
                    "bytes_processed": 0,
                    "fencing_token": 0,
                },
            )
            if (
                row.get("lease_owner") != kwargs["owner_id"]
                or int(row.get("fencing_token", -1)) != kwargs["fencing_token"]
                or row.get("lease_expires_at") is None
                or datetime.fromisoformat(row["lease_expires_at"]) <= datetime.now(UTC)
            ):
                return False
            row.update({"status": status, "error_code": kwargs.get("error_code")})
            return True

    async def source_sync_release_lease(
        self, agent_did: str, source_id: str, **kwargs: Any
    ) -> None:
        source_key = f"{agent_did}\0{source_id}"
        async with self._lock:
            row = self._tables.setdefault("connected_source_sync", {}).get(source_key)
            if (
                row is not None
                and row.get("lease_owner") == kwargs["owner_id"]
                and int(row.get("fencing_token", -1)) == kwargs["fencing_token"]
            ):
                row.pop("lease_owner", None)
                row.pop("lease_expires_at", None)

    async def source_sync_renew_lease(self, agent_did: str, source_id: str, **kwargs: Any) -> bool:
        source_key = f"{agent_did}\0{source_id}"
        async with self._lock:
            row = self._tables.setdefault("connected_source_sync", {}).get(source_key)
            if (
                row is None
                or row.get("lease_owner") != kwargs["owner_id"]
                or int(row.get("fencing_token", -1)) != kwargs["fencing_token"]
            ):
                return False
            row["lease_expires_at"] = (
                datetime.now(UTC) + timedelta(seconds=kwargs["ttl_seconds"])
            ).isoformat()
            return True

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
        async with self._lock:
            self._assert_fence_locked(fence)
            self._mutable[(collection, key)] = (copy.deepcopy(value), _now())
        _emit("mutable.write", collection, key, actor_did, sink)

    async def mutable_read(self, collection: str, key: str) -> dict[str, Any] | None:
        async with self._lock:
            item = self._mutable.get((collection, key))
            return _decode(item) if item is not None else None

    async def mutable_delete(
        self,
        collection: str,
        key: str,
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> bool:
        async with self._lock:
            deleted = self._mutable.pop((collection, key), None) is not None
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
        async with self._lock:
            rows = [
                _decode(item) for (name, _), item in self._mutable.items() if name == collection
            ]
        return rows if where is None else [row for row in rows if _matches(row, where)]

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
        async with self._lock:
            self._assert_fence_locked(fence)
            item = self._mutable.get((collection, key))
            if item is None:
                merged = False
            else:
                value, _ = item
                value.update(copy.deepcopy(patch))
                self._mutable[(collection, key)] = (value, _now())
                merged = True
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
        async with self._lock:
            self._assert_fence_locked(fence)
            won = self._update_if_locked(collection, key, patch, where, absent_where)
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
        async with self._lock:
            self._assert_fence_locked(fence)
            now = _now()
            for key, value in entries:
                self._mutable.setdefault((collection, key), (copy.deepcopy(value), now))
            rows = [_decode(self._mutable[(collection, key)]) for key, _ in entries]
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
        async with self._lock:
            self._assert_fence_locked(fence)
            item = self._mutable.get((collection, key))
            if item is None:
                incremented = False
            else:
                value, _ = item
                for path, delta in deltas.items():
                    _increment(value, path, delta)
                self._mutable[(collection, key)] = (value, _now())
                incremented = bool(deltas)
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
        async with self._lock:
            self._assert_fence_locked(fence)
            item = self._mutable.get((collection, key))
            if item is None or not _matches(item[0], where):
                won = False
            else:
                value, _ = item
                value.update(copy.deepcopy(patch))
                for path, delta in deltas.items():
                    _increment(value, path, delta)
                self._mutable[(collection, key)] = (value, _now())
                won = True
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
        async with self._lock:
            self._assert_fence_locked(fence)
            stored = self._mutable.get((collection, key))
            if stored is None:
                won = False
            else:
                value, _ = stored
                entries = value.setdefault(field, [])
                if item in entries:
                    won = False
                else:
                    entries.append(copy.deepcopy(item))
                    if length_field is not None:
                        value[length_field] = len(entries)
                    self._mutable[(collection, key)] = (value, _now())
                    won = True
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
        async with self._lock:
            won = self._update_if_locked(collection, key, patch, where, None)
            if won:
                self._tables.setdefault(APPROVAL_OUTBOX_TABLE, {}).setdefault(
                    event_id,
                    {
                        "record_id": event_id,
                        "approval_id": key,
                        "extra": copy.deepcopy(event),
                        "status": "pending",
                        "attempts": 0,
                    },
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
        async with self._lock:
            self._mutable[(collection, key)] = (copy.deepcopy(value), _now())
            self._tables.setdefault(APPROVAL_OUTBOX_TABLE, {}).setdefault(
                event_id,
                {
                    "record_id": event_id,
                    "approval_id": key,
                    "extra": copy.deepcopy(event),
                    "status": "pending",
                    "attempts": 0,
                },
            )
        _emit("approval.create", collection, key, actor_did, sink)

    async def claim_outbox(self, consumer_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("outbox claim limit must be positive")
        async with self._lock:
            ready = [
                row
                for row in self._tables.get(APPROVAL_OUTBOX_TABLE, {}).values()
                if row["status"] == "pending"
            ][:limit]
            for row in ready:
                row["status"] = "leased"
                row["lease_owner"] = consumer_id
                row["attempts"] += 1
            return [
                {
                    "event_id": row["record_id"],
                    "approval_id": row["approval_id"],
                    "event": copy.deepcopy(row["extra"]),
                    "attempts": row["attempts"],
                }
                for row in ready
            ]

    async def ack_outbox(self, consumer_id: str, event_ids: Sequence[str]) -> None:
        async with self._lock:
            for event_id in event_ids:
                row = self._tables.get(APPROVAL_OUTBOX_TABLE, {}).get(event_id)
                if row is not None and row.get("lease_owner") == consumer_id:
                    row["status"] = "delivered"
                    row.pop("lease_owner", None)

    async def nack_outbox(
        self,
        consumer_id: str,
        event_id: str,
        *,
        retry_after_seconds: float,
    ) -> bool:
        if retry_after_seconds < 0:
            raise ValueError("retry delay must not be negative")
        async with self._lock:
            row = self._tables.get(APPROVAL_OUTBOX_TABLE, {}).get(event_id)
            if row is None or row.get("lease_owner") != consumer_id:
                return False
            row["status"] = "pending"
            row.pop("lease_owner", None)
            return True

    async def reject_outbox(self, consumer_id: str, event_id: str) -> bool:
        async with self._lock:
            row = self._tables.get(APPROVAL_OUTBOX_TABLE, {}).get(event_id)
            if row is None or row.get("lease_owner") != consumer_id:
                return False
            row["status"] = "delivered"
            row["rejected"] = True
            row.pop("lease_owner", None)
            return True

    async def enqueue_mail(self, event_id: str, envelope: dict[str, Any]) -> None:
        async with self._lock:
            self._tables.setdefault(MAIL_OUTBOX_TABLE, {}).setdefault(
                event_id,
                {
                    "event_id": event_id,
                    "envelope": copy.deepcopy(envelope),
                    "status": "pending",
                    "attempts": 0,
                },
            )

    async def claim_mail(self, consumer_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("limit must be positive")
        async with self._lock:
            rows = [
                row for row in self._tables.get(MAIL_OUTBOX_TABLE, {}).values()
                if row["status"] == "pending"
                or (row["status"] == "leased" and row.get("lease_until", 0) <= time.monotonic())
            ][:limit]
            for row in rows:
                row["status"] = "leased"
                row["lease_owner"] = consumer_id
                row["lease_until"] = time.monotonic() + 60
                row["attempts"] += 1
            return [copy.deepcopy(row) for row in rows]

    async def ack_mail(self, consumer_id: str, event_id: str) -> bool:
        async with self._lock:
            row = self._tables.get(MAIL_OUTBOX_TABLE, {}).get(event_id)
            if row is None or row.get("lease_owner") != consumer_id:
                return False
            row["status"] = "delivered"
            row.pop("lease_owner", None)
            return True

    async def nack_mail(
        self, consumer_id: str, event_id: str, *, retry_after_seconds: float
    ) -> bool:
        if retry_after_seconds < 0:
            raise ValueError("retry delay must not be negative")
        async with self._lock:
            row = self._tables.get(MAIL_OUTBOX_TABLE, {}).get(event_id)
            if row is None or row.get("lease_owner") != consumer_id:
                return False
            row["status"] = "pending"
            row.pop("lease_owner", None)
            return True

    def _update_if_locked(
        self,
        collection: str,
        key: str,
        patch: dict[str, Any],
        where: dict[str, Any],
        absent_where: dict[str, Any] | None,
    ) -> bool:
        item = self._mutable.get((collection, key))
        if item is None or not _matches(item[0], where):
            return False
        if absent_where and any(
            collection == name and other_key != key and _matches(value, absent_where)
            for (name, other_key), (value, _) in self._mutable.items()
        ):
            return False
        value, _ = item
        value.update(copy.deepcopy(patch))
        self._mutable[(collection, key)] = (value, _now())
        return True


def _decode(item: tuple[dict[str, Any], str]) -> dict[str, Any]:
    value, updated_at = item
    decoded = copy.deepcopy(value)
    decoded["updated_at"] = updated_at
    return decoded


def _emit(
    action: str,
    collection: str,
    key: str,
    actor_did: str,
    sink: Any | None,
    outcome: str = "applied",
) -> None:
    if sink is not None:
        emit(
            AuditEvent(
                actor_did=actor_did, action=action, target=f"{collection}/{key}", outcome=outcome
            ),
            sink,
        )


def _parse_order_by(order_by: str) -> tuple[str, bool]:
    parts = order_by.split()
    if len(parts) > 2 or not parts:
        raise ValueError("invalid order_by")
    return parts[0], len(parts) == 2 and parts[1].upper() == "DESC"
