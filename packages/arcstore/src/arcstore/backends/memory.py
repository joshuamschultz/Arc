"""In-memory implementation of the complete ArcStore backend contract."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from arctrust.audit import AuditEvent, emit

from arcstore.backends.base import APPROVAL_OUTBOX_TABLE


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


class FakeBackend:
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

    async def mutable_write(
        self,
        collection: str,
        key: str,
        value: dict[str, Any],
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> None:
        async with self._lock:
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
    ) -> bool:
        async with self._lock:
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
    ) -> bool:
        async with self._lock:
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
    ) -> list[dict[str, Any]]:
        async with self._lock:
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
    ) -> bool:
        async with self._lock:
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
    ) -> bool:
        async with self._lock:
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
    ) -> bool:
        async with self._lock:
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
