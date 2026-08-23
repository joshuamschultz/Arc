"""Read-only local SQLite connected-datastore adapter."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    ListSourceResources,
    SelectSourceResources,
    SourceContent,
    SourceDescription,
    SourceError,
    SourceFailureCode,
    SourceResource,
    SyncSource,
    SyncSourcePage,
)


class SQLiteSourceAdapter:
    def __init__(self, database_path: Path) -> None:
        self._path = Path(database_path).resolve(strict=True)
        if not self._path.is_file() or self._path.is_symlink():
            raise ValueError("SQLite source must be an approved regular file")
        self._selected: set[str] = set()
        self._conn: sqlite3.Connection | None = None

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="sqlite",
            account_id=hashlib.sha256(str(self._path).encode()).hexdigest()[:24],
            display_name="SQLite database",
            supports_incremental=False,
            supports_deletes=False,
        )

    async def list_source_resources(
        self, request: ListSourceResources
    ) -> tuple[SourceResource, ...]:
        del request
        rows = (
            self._connection()
            .execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            .fetchall()
        )
        return tuple(
            SourceResource(
                resource_id=str(row[0]),
                label=str(row[0]),
                resource_kind="table",
                selected=str(row[0]) in self._selected,
            )
            for row in rows
        )

    async def select_source_resources(self, request: SelectSourceResources) -> None:
        available = {
            item.resource_id
            for item in await self.list_source_resources(
                ListSourceResources(connection_id=request.connection_id)
            )
        }
        if not set(request.resource_ids).issubset(available):
            raise SourceError(SourceFailureCode.NOT_FOUND, "selected SQLite table is unavailable")
        self._selected = set(request.resource_ids)

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        return SyncSourcePage(
            next_checkpoint=request.checkpoint or self._fingerprint(), has_more=False
        )

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        raise SourceError(SourceFailureCode.UNSUPPORTED_CONTENT, "SQLite is queried live")

    async def close_source(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    async def datastore_port(self) -> SQLiteSourceAdapter:
        return self

    async def introspect(self) -> Any:
        from arcmemory.datastore import SqliteDatastore

        return SqliteDatastore(self._connection()).introspect()

    async def persist_ontology(self, store: Any) -> None:
        from arcmemory.datastore import SqliteDatastore

        SqliteDatastore(self._connection()).persist_ontology(store)

    async def query(self, op: str, table: str, args: dict[str, object]) -> object:
        if self._selected and table not in self._selected:
            raise ValueError("unknown or unapproved SQLite table")
        from arcmemory.datastore import SqliteDatastore

        return SqliteDatastore(self._connection()).query(op, table, args)

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
        return self._conn

    def _fingerprint(self) -> str:
        stat = self._path.stat()
        return f"{stat.st_ino}:{stat.st_mtime_ns}:{stat.st_size}"


def build_source_adapter(context: dict[str, Any]) -> SQLiteSourceAdapter:
    return SQLiteSourceAdapter(Path(context["database_path"]))
