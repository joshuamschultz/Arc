"""Optional PostgreSQL/Supabase source and live typed datastore adapter.

The connection URL is received only from Arc's secret capability at construction.
Tables and columns are discovered from the read-only connection, then every later
identifier is checked against that discovery before it is quoted into SQL. Values
are always driver-bound parameters; this extension deliberately has no raw-SQL API.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import re
from typing import Any
from urllib.parse import urlparse

from arcagent.extension.attachment import (
    ProbeResult,
    Requirement,
    RequirementKind,
    ToolOutcome,
    ToolResult,
    ToolSpec,
)
from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    ListSourceResources,
    SelectSourceResources,
    SourceContent,
    SourceDataShape,
    SourceDescription,
    SourceError,
    SourceFailureCode,
    SourceResource,
    SyncSource,
    SyncSourcePage,
)

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z")
_TEXT_TYPES = ("char", "text", "json", "xml", "uuid")
_MAX_LIMIT = 200
_TIMEOUT_SECONDS = 20.0


class PostgreSQLAttachment:
    """A bounded read-only connector that also satisfies ArcMemory's datastore port."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._pool: Any | None = None
        self._pool_lock = asyncio.Lock()
        self._ontology: Any | None = None
        self._selected: set[str] = set()
        self._driver: Any | None = None

    def requirements(self) -> list[Requirement]:
        """Require the vault-resolved read-only connection capability."""
        return [
            Requirement(
                kind=RequirementKind.CREDENTIAL,
                name="database_url",
                instruction="A read-only PostgreSQL or Supabase connection URL",
            )
        ]

    async def probe(self) -> ProbeResult:
        """Verify the scoped connection without returning connection details."""
        if not self._database_url:
            return ProbeResult(reachable=False, detail="postgresql has no database_dsn credential")
        try:
            await self._execute("SELECT 1")
        except (OSError, ConnectionError, TimeoutError, SourceError) as exc:
            return ProbeResult(
                reachable=False, detail=f"PostgreSQL did not answer: {type(exc).__name__}"
            )
        return ProbeResult(
            reachable=True,
            tools=await self.describe_tools(),
            detail="reached PostgreSQL with a scoped read-only connection",
        )

    async def describe_tools(self) -> list[ToolSpec]:
        """Expose only typed reads; raw SQL is intentionally not a connector verb."""
        string = {"type": "string"}
        return [
            ToolSpec(
                name="postgres_schema",
                description="List approved PostgreSQL schemas and tables.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                classification="read_only",
            ),
            ToolSpec(
                name="postgres_get",
                description="Get one record by primary key from an approved table.",
                input_schema=_schema({"table": string, "pk_value": string}, ("table", "pk_value")),
                classification="read_only",
            ),
            ToolSpec(
                name="postgres_find",
                description="Find records by equality in an approved table column.",
                input_schema=_schema(
                    {"table": string, "column": string, "value": string, "limit": string},
                    ("table", "column", "value"),
                ),
                classification="read_only",
            ),
            ToolSpec(
                name="postgres_list",
                description="List a bounded number of records from an approved table.",
                input_schema=_schema({"table": string, "limit": string}, ("table",)),
                classification="read_only",
            ),
        ]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        """Dispatch one declared typed query and return only rendered data."""
        try:
            if tool == "postgres_schema":
                ontology = await self.introspect()
                content = ", ".join(sorted(ontology.tables)) or "No approved tables found."
            elif tool == "postgres_get":
                content = str(
                    await self.query(
                        "get_record",
                        _string(args, "table"),
                        {"pk_value": _string(args, "pk_value")},
                    )
                )
            elif tool == "postgres_find":
                content = str(
                    await self.query(
                        "find",
                        _string(args, "table"),
                        {
                            "column": _string(args, "column"),
                            "value": _string(args, "value"),
                            "limit": args.get("limit", _MAX_LIMIT),
                        },
                    )
                )
            elif tool == "postgres_list":
                content = str(
                    await self.query(
                        "list", _string(args, "table"), {"limit": args.get("limit", _MAX_LIMIT)}
                    )
                )
            else:
                return ToolResult(
                    tool=tool, outcome=ToolOutcome.ERROR, content="unknown PostgreSQL tool"
                )
        except (SourceError, ValueError, KeyError) as exc:
            return ToolResult(tool=tool, outcome=ToolOutcome.ERROR, content=str(exc))
        return ToolResult(tool=tool, content=content)

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        """Describe a stable opaque database account, never a URL or credential."""
        parsed = urlparse(self._database_url)
        account_material = f"{parsed.hostname or ''}\0{parsed.path.strip('/')}"
        account_id = hashlib.sha256(account_material.encode()).hexdigest()[:24]
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="postgres",
            account_id=account_id,
            data_shape=SourceDataShape.DATASTORE,
            display_name="PostgreSQL database",
            supports_incremental=False,
            supports_deletes=False,
            root_locator=",".join(sorted(self._selected)),
        )

    async def list_source_resources(
        self, request: ListSourceResources
    ) -> tuple[SourceResource, ...]:
        """Discover non-system tables that an operator may map and approve."""
        del request
        ontology = await self.introspect()
        return tuple(
            SourceResource(
                resource_id=name,
                label=name,
                resource_kind="table",
                locator=name,
                selected=name in self._selected,
            )
            for name in sorted(ontology.tables)
        )

    async def select_source_resources(self, request: SelectSourceResources) -> None:
        """Persist only explicitly discovered table selections in this live adapter."""
        available = {
            resource.resource_id
            for resource in await self.list_source_resources(
                ListSourceResources(connection_id=request.connection_id)
            )
        }
        if not set(request.resource_ids).issubset(available):
            raise SourceError(
                SourceFailureCode.NOT_FOUND, "selected PostgreSQL table is unavailable"
            )
        self._selected = set(request.resource_ids)

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        """Datastores stay live; synchronization only establishes the mapping checkpoint."""
        return SyncSourcePage(next_checkpoint=request.checkpoint or "live", has_more=False)

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        """Datastore rows never flow through a document/blob fetch path."""
        raise SourceError(SourceFailureCode.UNSUPPORTED_CONTENT, "PostgreSQL is queried live")

    async def close_source(self) -> None:
        """Release the optional driver pool when the granted connection is removed."""
        async with self._pool_lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()

    async def datastore_port(self) -> PostgreSQLAttachment:
        """Return this structural ``DatastorePort`` after its schema has been bounded."""
        await self.introspect()
        return self

    async def introspect(self) -> Any:
        """Build the typed ontology from information_schema, constrained to selected tables."""
        if self._ontology is not None:
            return self._ontology
        rows = await self._execute(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_type = 'BASE TABLE' "
            "AND table_schema NOT IN ('information_schema', 'pg_catalog') "
            "ORDER BY table_schema, table_name"
        )
        names = tuple(f"{row['table_schema']}.{row['table_name']}" for row in rows)
        if self._selected:
            names = tuple(name for name in names if name in self._selected)
        tables: dict[str, Any] = {}
        entity_map: dict[str, str] = {}
        datastore = importlib.import_module("arcmemory.datastore")
        for table in names:
            schema, name = _split_table(table)
            column_rows = await self._execute(
                "SELECT column_name, data_type, ordinal_position FROM information_schema.columns "
                "WHERE table_schema = $1 AND table_name = $2 ORDER BY ordinal_position",
                schema,
                name,
            )
            pk_rows = await self._execute(
                "SELECT kcu.column_name FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "ON tc.constraint_name = kcu.constraint_name "
                "AND tc.table_schema = kcu.table_schema "
                "WHERE tc.constraint_type = 'PRIMARY KEY' "
                "AND tc.table_schema = $1 AND tc.table_name = $2 "
                "ORDER BY kcu.ordinal_position",
                schema,
                name,
            )
            columns = [str(row["column_name"]) for row in column_rows]
            searchable = [
                str(row["column_name"])
                for row in column_rows
                if any(kind in str(row["data_type"]).lower() for kind in _TEXT_TYPES)
            ]
            primary_key = str(pk_rows[0]["column_name"]) if len(pk_rows) == 1 else None
            tables[table] = datastore.TableInfo(
                name=table,
                primary_key=primary_key,
                columns=columns,
                searchable_columns=searchable,
                foreign_keys={},
            )
            entity_map[_singularize(name)] = f"table:{table}"
        self._ontology = datastore.DatastoreOntology(tables=tables, entity_map=entity_map)
        return self._ontology

    async def persist_ontology(self, store: Any) -> None:
        """Persist safe table shape facts without copying database rows into memory."""
        ontology = await self.introspect()
        for name, info in ontology.tables.items():
            slug = "db-table-" + name.replace(".", "-")
            store.write_fact(
                slug, "primary_key", info.primary_key or "", name=name, entity_type="db_table"
            )
            store.write_fact(
                slug,
                "searchable_columns",
                ", ".join(info.searchable_columns),
                entity_type="db_table",
            )

    async def query(self, op: str, table: str, args: dict[str, object]) -> object:
        """Run one allowlisted, parameterized typed read against an approved table."""
        info = await self._table_info(table)
        quoted_table = _quoted_table(table)
        limit = _limit(args.get("limit", 50))
        if op == "get_record":
            if info.primary_key is None:
                raise ValueError(f"table {table!r} has no single-column primary key")
            rows = await self._execute(
                f"SELECT * FROM {quoted_table} WHERE {_quote(info.primary_key)} = $1 LIMIT 1",  # noqa: S608 - schema-validated identifiers
                str(args["pk_value"]),
            )
            return _record(rows[0]) if rows else None
        if op == "find":
            column = str(args["column"])
            if column not in info.columns:
                raise ValueError(f"unknown column {column!r} on table {table!r}")
            rows = await self._execute(
                f"SELECT * FROM {quoted_table} WHERE {_quote(column)} = $1 LIMIT $2",  # noqa: S608 - schema-validated identifiers
                str(args["value"]),
                limit,
            )
            return [_record(row) for row in rows]
        if op == "list":
            rows = await self._execute(
                f"SELECT * FROM {quoted_table} LIMIT $1",  # noqa: S608 - schema-validated identifier
                limit,
            )
            return [_record(row) for row in rows]
        raise ValueError(f"unknown datastore op: {op!r}")

    async def _table_info(self, table: str) -> Any:
        ontology = await self.introspect()
        info = ontology.tables.get(table)
        if info is None:
            raise ValueError(f"unknown or unapproved table: {table!r}")
        return info

    async def _execute(self, query: str, *args: object) -> list[Any]:
        """Execute a fixed/query-builder-owned statement with retry after pool loss."""
        for attempt in range(2):
            pool = await self._open_pool()
            try:
                async with asyncio.timeout(_TIMEOUT_SECONDS):
                    async with pool.acquire() as connection:
                        return list(await connection.fetch(query, *args))
            except (OSError, ConnectionError, TimeoutError) as exc:
                if attempt:
                    raise SourceError(
                        SourceFailureCode.TRANSIENT, "PostgreSQL connection failed"
                    ) from exc
                await self._drop_pool(pool)
        raise SourceError(SourceFailureCode.TRANSIENT, "PostgreSQL connection failed")

    async def _open_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        async with self._pool_lock:
            if self._pool is not None:
                return self._pool
            try:
                driver = importlib.import_module("asyncpg")
            except ImportError as exc:
                raise SourceError(
                    SourceFailureCode.UNSUPPORTED_CONTENT,
                    "PostgreSQL support requires the optional asyncpg dependency",
                ) from exc
            parsed = urlparse(self._database_url)
            ssl: str | None = (
                None if parsed.hostname in {"localhost", "127.0.0.1", "::1"} else "require"
            )
            self._driver = driver
            self._pool = await driver.create_pool(
                dsn=self._database_url,
                min_size=1,
                max_size=4,
                command_timeout=_TIMEOUT_SECONDS,
                statement_cache_size=0,
                ssl=ssl,
            )
            return self._pool

    async def _drop_pool(self, pool: Any) -> None:
        async with self._pool_lock:
            if self._pool is pool:
                self._pool = None
        await pool.close()


def build_native_attachment(context: dict[str, Any]) -> PostgreSQLAttachment:
    """Build from Arc's ephemeral vault-reveal context; never persist credentials."""
    return PostgreSQLAttachment(str(context.get("database_dsn", "")))


def _schema(properties: dict[str, dict[str, str]], required: tuple[str, ...]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _string(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    return value


def _split_table(table: str) -> tuple[str, str]:
    parts = table.split(".", 1)
    if len(parts) != 2 or not all(_IDENTIFIER.fullmatch(part) for part in parts):
        raise ValueError("invalid PostgreSQL table identifier")
    return parts[0], parts[1]


def _quote(identifier: str) -> str:
    if _IDENTIFIER.fullmatch(identifier) is None:
        raise ValueError("invalid PostgreSQL identifier")
    return f'"{identifier}"'


def _quoted_table(table: str) -> str:
    schema, name = _split_table(table)
    return f"{_quote(schema)}.{_quote(name)}"


def _limit(value: object) -> int:
    try:
        return max(1, min(int(str(value)), _MAX_LIMIT))
    except ValueError as exc:
        raise ValueError("limit must be an integer") from exc


def _record(row: Any) -> dict[str, object]:
    return {str(key): row[key] for key in row.keys()}


def _singularize(name: str) -> str:
    return name[:-1] if name.endswith("s") and len(name) > 1 else name


__all__ = ["PostgreSQLAttachment", "build_native_attachment"]
