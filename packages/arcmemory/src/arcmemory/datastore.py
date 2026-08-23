"""Backend-neutral structured-data port plus the optional SQLite adapter.

Dependency-free: introspection uses only stdlib :mod:`sqlite3` PRAGMA calls
(``table_info`` / ``foreign_key_list``). SQLAlchemy is a future opt-in
generalization for non-sqlite backends and is never imported here.

Every table/column identifier interpolated into a SQL string below is first
checked against the schema this class itself introspected via PRAGMA — never
against agent-supplied free text — so no identifier is attacker-controlled.
Every value is passed as a bound parameter. No public method accepts a raw
SQL string to execute verbatim (enforced by
``test_no_public_method_accepts_a_raw_sql_string_parameter``).
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel

if TYPE_CHECKING:
    from arcmemory.stores.semantic import SemanticStore

#: Declared sqlite column types treated as free-text search targets.
_TEXT_TYPES = ("TEXT", "CHAR", "CLOB", "VARCHAR")

#: query() op name -> the keys of the structured-data port's args dict it consumes.
_KNOWN_OPS = frozenset({"get_record", "find", "list"})


class TableInfo(BaseModel):
    """Introspected shape of one sqlite table."""

    name: str
    primary_key: str | None
    columns: list[str]
    searchable_columns: list[str]
    foreign_keys: dict[str, str]


class DatastoreOntology(BaseModel):
    """The full introspected schema of a datastore: tables + a naming lookup."""

    tables: dict[str, TableInfo]
    entity_map: dict[str, str]


def _singularize(table_name: str) -> str:
    """Naive singular noun for the entity map (``invoices`` -> ``invoice``)."""
    if table_name.endswith("s") and len(table_name) > 1:
        return table_name[:-1]
    return table_name


def _int_arg(args: dict[str, object], key: str, default: int) -> int:
    """``args[key]`` coerced to ``int`` (bound-parameter value, not SQL) or ``default``."""
    value = args.get(key, default)
    return int(value) if isinstance(value, int | float | str) else default


@runtime_checkable
class DatastorePort(Protocol):
    """Typed asynchronous read port for an approved structured data source.

    Implementations own their driver and credentials.  The agent can select an
    allowlisted operation and schema member, never submit arbitrary SQL.
    """

    async def introspect(self) -> DatastoreOntology: ...

    async def persist_ontology(self, store: SemanticStore) -> None: ...

    async def query(self, op: str, table: str, args: dict[str, object]) -> object: ...


class SqliteDatastore:
    """Read-only sqlite introspection + parameterized, allowlisted read ops.

    Accepts a caller-provided connection and never issues writes. Meant for a
    connection opened read-only (e.g. ``sqlite3.connect(f"file:{path}?mode=ro",
    uri=True)``); this class itself only ever executes SELECT/PRAGMA/COUNT.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._ontology: DatastoreOntology | None = None

    def introspect(self) -> DatastoreOntology:
        """Build the ontology from stdlib PRAGMA table_info / foreign_key_list."""
        table_names = [
            str(row[0])
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        tables: dict[str, TableInfo] = {}
        entity_map: dict[str, str] = {}
        for table_name in table_names:
            tables[table_name] = self._table_info_for(table_name)
            entity_map[_singularize(table_name)] = f"table:{table_name}"
        ontology = DatastoreOntology(tables=tables, entity_map=entity_map)
        self._ontology = ontology
        return ontology

    def _table_info_for(self, table_name: str) -> TableInfo:
        """PRAGMA table_info/foreign_key_list for one table already known to exist.

        ``table_name`` here always comes from ``sqlite_master`` (queried just
        above), never from a caller — PRAGMA does not accept bound parameters
        for identifiers, so this interpolation is safe by construction.
        """
        columns: list[str] = []
        searchable: list[str] = []
        primary_key: str | None = None
        for col in self._conn.execute(f"PRAGMA table_info({table_name})"):
            col_name = str(col[1])
            col_type = str(col[2] or "").upper()
            columns.append(col_name)
            if any(text_type in col_type for text_type in _TEXT_TYPES):
                searchable.append(col_name)
            if col[5]:
                primary_key = col_name
        foreign_keys: dict[str, str] = {}
        for fk in self._conn.execute(f"PRAGMA foreign_key_list({table_name})"):
            foreign_keys[str(fk[3])] = str(fk[2])
        return TableInfo(
            name=table_name,
            primary_key=primary_key,
            columns=columns,
            searchable_columns=searchable,
            foreign_keys=foreign_keys,
        )

    def persist_ontology(self, store: SemanticStore) -> None:
        """Write each table as a ``db_table`` Entity — Facts: primary_key, row_count,
        searchable_columns, and one fact per foreign key. Entity+Fact projection,
        glass-box (COMP-008)."""
        ontology = self._ontology or self.introspect()
        for name, info in ontology.tables.items():
            slug = f"db-table-{name}"
            store.write_fact(
                slug, "primary_key", info.primary_key or "", name=name, entity_type="db_table"
            )
            store.write_fact(slug, "row_count", str(self._row_count(name)), entity_type="db_table")
            store.write_fact(
                slug,
                "searchable_columns",
                ", ".join(info.searchable_columns),
                entity_type="db_table",
            )
            for column, referenced_table in info.foreign_keys.items():
                store.write_fact(slug, f"fk:{column}", referenced_table, entity_type="db_table")

    def _row_count(self, table: str) -> int:
        """COUNT(*) for a table name already validated by introspection."""
        row = self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608
        return int(row[0]) if row else 0

    def _validated_table(self, table: str) -> TableInfo:
        """The introspected schema for ``table``, or ValueError if it is not real."""
        ontology = self._ontology or self.introspect()
        info = ontology.tables.get(table)
        if info is None:
            raise ValueError(f"unknown table: {table!r}")
        return info

    def get_record(self, table: str, pk_value: str) -> dict[str, object] | None:
        """Parameterized ``SELECT * WHERE <pk>=?`` — exact lookup by primary key.

        ``table`` is validated against the introspected schema before use in the
        SQL string; ``pk_value`` is always a bound parameter.
        """
        info = self._validated_table(table)
        if info.primary_key is None:
            raise ValueError(f"table {table!r} has no primary key")
        cursor = self._conn.execute(
            f"SELECT * FROM {table} WHERE {info.primary_key}=?",  # noqa: S608
            (pk_value,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [str(d[0]) for d in cursor.description]
        return dict(zip(columns, row, strict=True))

    def find(
        self, table: str, column: str, value: str, *, limit: int = 50
    ) -> list[dict[str, object]]:
        """Parameterized equality search, row-capped. ``table``/``column`` are
        validated against the introspected schema; ``value`` is a bound parameter."""
        info = self._validated_table(table)
        if column not in info.columns:
            raise ValueError(f"unknown column {column!r} on table {table!r}")
        cursor = self._conn.execute(
            f"SELECT * FROM {table} WHERE {column}=? LIMIT ?",  # noqa: S608
            (value, limit),
        )
        columns = [str(d[0]) for d in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def list(self, table: str, *, limit: int = 50) -> list[dict[str, object]]:
        """First ``limit`` rows of ``table``. ``table`` is schema-validated."""
        self._validated_table(table)
        cursor = self._conn.execute(f"SELECT * FROM {table} LIMIT ?", (limit,))  # noqa: S608
        columns = [str(d[0]) for d in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def query(self, op: str, table: str, args: dict[str, object]) -> object:
        """Dispatch to get_record/find/list by name; unknown op -> ValueError.

        No raw SQL travels through here — ``op`` selects one of the three typed
        methods above and ``args`` supplies only their already-typed parameters.
        """
        if op not in _KNOWN_OPS:
            raise ValueError(f"unknown datastore op: {op!r}")
        if op == "get_record":
            return self.get_record(table, str(args["pk_value"]))
        if op == "find":
            return self.find(
                table,
                str(args["column"]),
                str(args["value"]),
                limit=_int_arg(args, "limit", 50),
            )
        return self.list(table, limit=_int_arg(args, "limit", 50))


class SqliteDatastorePort:
    """Optional SQLite implementation of :class:`DatastorePort`.

    SQLite calls remain confined to this adapter.  Network database adapters
    implement the same async port without changing Brain or agent tools.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._store = SqliteDatastore(conn)

    async def introspect(self) -> DatastoreOntology:
        return self._store.introspect()

    async def persist_ontology(self, store: SemanticStore) -> None:
        self._store.persist_ontology(store)

    async def query(self, op: str, table: str, args: dict[str, object]) -> object:
        return self._store.query(op, table, args)


__all__ = [
    "DatastoreOntology",
    "DatastorePort",
    "SqliteDatastore",
    "SqliteDatastorePort",
    "TableInfo",
]
