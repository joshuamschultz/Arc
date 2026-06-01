"""FakeBackend — in-memory StorageBackend used in tests (FR-3 AC-3.4).

Pure Python dicts; no SQLite, no driver. Running the Protocol conformance
suite against this alongside ``SqliteBackend`` proves the contract leaks no
SQLite-specific types.
"""

from __future__ import annotations

import copy
from typing import Any


class FakeBackend:
    """Dict-backed StorageBackend. First write per key wins (idempotent)."""

    def __init__(self) -> None:
        self._tables: dict[str, dict[str, dict[str, Any]]] = {}
        self._cursors: dict[str, int] = {}

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def upsert(self, table: str, key: str, row: dict[str, Any]) -> None:
        self._tables.setdefault(table, {}).setdefault(key, copy.deepcopy(row))

    async def upsert_many(self, table: str, rows: list[tuple[str, dict[str, Any]]]) -> None:
        bucket = self._tables.setdefault(table, {})
        for key, row in rows:
            bucket.setdefault(key, copy.deepcopy(row))

    async def query(
        self,
        table: str,
        *,
        where: dict[str, Any] | None = None,
        order_by: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = [copy.deepcopy(r) for r in self._tables.get(table, {}).values()]
        if where:
            rows = [r for r in rows if all(r.get(k) == v for k, v in where.items())]
        if order_by:
            column, reverse = _parse_order_by(order_by)
            rows.sort(key=lambda r: (r.get(column) is None, r.get(column)), reverse=reverse)
        if limit is not None:
            rows = rows[:limit]
        return rows

    async def get_cursor(self, name: str) -> int:
        return self._cursors.get(name, 0)

    async def set_cursor(self, name: str, value: int) -> None:
        self._cursors[name] = value


def _parse_order_by(order_by: str) -> tuple[str, bool]:
    parts = order_by.split()
    column = parts[0]
    reverse = len(parts) > 1 and parts[1].upper() == "DESC"
    return column, reverse
