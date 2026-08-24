"""Read-only SQLite connector: interactive tools plus the connected-datastore seam.

A SQLite database is one file, which makes two things true that shape everything
here.

**It may not be on this machine.** The agent runs where the fleet runs; the
database is wherever the person made it. So a connection names an optional
``host`` alongside the path, and when one is given the file is copied here over
``ssh`` and queried locally. Nothing is executed remotely but ``stat`` and
``cat`` — the remote side is a file server, not a query engine.

**It is queried live, never ingested.** Rows are not documents; copying them into
memory would make a stale duplicate of something already authoritative. So the
source half of this adapter establishes the mapping and returns no objects, and
every read goes through the typed, parameterized query path in
``arcmemory.datastore`` — this module has no API that executes a SQL string.

What an agent is *shown* comes from the operator's semantic layer
(:mod:`arcmemory.semantic_layer`): the names and sentences that say what
``inv_hdr.amt`` actually is. What an agent may *reach* is the approved table
selection. Those are deliberately separate mechanisms.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shlex
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

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

#: Rows one typed read may return. A datastore answer is read by a model, and a
#: model reading ten thousand rows is a context bill, not an answer.
_MAX_LIMIT = 200

#: How long any single ssh call may take. A host that is off must fail as "not
#: reachable" in seconds, not hold a connections page until something else
#: times out.
_SSH_TIMEOUT_SECONDS = 60.0

#: The largest remote database this will copy. A SQLite file has no streaming
#: read, so fetching one means having all of it — an unbounded copy is an
#: unbounded write to this machine's disk.
_MAX_REMOTE_BYTES = 2 * 1024 * 1024 * 1024

#: ssh options that keep this non-interactive. BatchMode turns a missing key into
#: an immediate failure instead of a password prompt no one is there to answer.
_SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")


class SQLiteAttachment:
    """A bounded read-only SQLite connector that also satisfies ArcMemory's ports.

    One object rather than two because a connection is one account: the tools an
    agent calls and the knowledge enrollment an operator configures must see the
    same tables, the same approvals and the same semantic layer. Splitting them
    was how a connector came to answer tool calls while Knowledge showed nothing.
    """

    def __init__(self, database_path: str, host: str = "", connection_id: str = "") -> None:
        self._remote_path = database_path.strip()
        self._host = host.strip()
        self._connection_id = connection_id
        self._selected: set[str] = set()
        self._ontology: Any = None
        self._cache_dir: Path | None = None
        self._local: Path | None = None
        self._fingerprint = ""
        self._schema_fingerprint = ""
        self._lock = asyncio.Lock()

    # --- the interactive tool half ------------------------------------------

    def requirements(self) -> list[Requirement]:
        """Declare what an operator must supply for this connection to work."""
        return [
            Requirement(
                kind=RequirementKind.CREDENTIAL,
                name="database_path",
                instruction="Absolute path to the SQLite file, on whichever machine holds it",
            )
        ]

    async def probe(self) -> ProbeResult:
        """Confirm the file is reachable and readable, and say which one it is not.

        The two failures an operator actually hits are a path typo and an
        unreachable host, and they need different fixes — so they are reported
        as different sentences rather than one "could not connect".
        """
        if not self._remote_path:
            return ProbeResult(
                reachable=False,
                detail=(
                    "sqlite is not configured: database_path is required, and host is "
                    "optional (leave it empty when the file is on this machine)"
                ),
            )
        if self._host and shutil.which("ssh") is None:
            return ProbeResult(
                reachable=False,
                detail=f"database_path names host {self._host!r} but ssh is not installed here",
            )
        try:
            await self._ensure_local()
        except SourceError as exc:
            return ProbeResult(reachable=False, detail=exc.detail)
        return ProbeResult(
            reachable=True,
            tools=await self.describe_tools(),
            detail=f"opened {self._where()} read-only",
        )

    async def describe_tools(self) -> list[ToolSpec]:
        """Expose only typed reads; raw SQL is deliberately not a connector verb."""
        string = {"type": "string"}
        return [
            ToolSpec(
                name="sqlite_schema",
                description=(
                    "Describe the approved SQLite tables — what one row of each means, "
                    "its columns and which are searchable. Read this before querying."
                ),
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                classification="read_only",
            ),
            ToolSpec(
                name="sqlite_get",
                description="Get one record by primary key from an approved SQLite table.",
                input_schema=_schema({"table": string, "pk_value": string}, ("table", "pk_value")),
                classification="read_only",
            ),
            ToolSpec(
                name="sqlite_find",
                description="Find records by equality in an approved SQLite column.",
                input_schema=_schema(
                    {"table": string, "column": string, "value": string, "limit": string},
                    ("table", "column", "value"),
                ),
                classification="read_only",
            ),
            ToolSpec(
                name="sqlite_list",
                description="List a bounded number of records from an approved SQLite table.",
                input_schema=_schema({"table": string, "limit": string}, ("table",)),
                classification="read_only",
            ),
        ]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        """Dispatch one declared typed query and return only rendered data."""
        try:
            if tool == "sqlite_schema":
                content = await self._describe_schema()
            elif tool == "sqlite_get":
                content = str(
                    await self.query(
                        "get_record",
                        _string(args, "table"),
                        {"pk_value": _string(args, "pk_value")},
                    )
                )
            elif tool == "sqlite_find":
                content = str(
                    await self.query(
                        "find",
                        _string(args, "table"),
                        {
                            "column": _string(args, "column"),
                            "value": _string(args, "value"),
                            "limit": _limit(args.get("limit", _MAX_LIMIT)),
                        },
                    )
                )
            elif tool == "sqlite_list":
                content = str(
                    await self.query(
                        "list",
                        _string(args, "table"),
                        {"limit": _limit(args.get("limit", _MAX_LIMIT))},
                    )
                )
            else:
                return ToolResult(
                    tool=tool, outcome=ToolOutcome.ERROR, content="unknown SQLite tool"
                )
        except (SourceError, ValueError, KeyError, sqlite3.Error) as exc:
            return ToolResult(tool=tool, outcome=ToolOutcome.ERROR, content=str(exc))
        return ToolResult(tool=tool, content=content)

    # --- the connected-source half ------------------------------------------

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        """Describe a stable opaque database identity, never a path or a host."""
        material = f"{self._host}\0{self._remote_path}"
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="sqlite",
            account_id=hashlib.sha256(material.encode()).hexdigest()[:24],
            data_shape=SourceDataShape.DATASTORE,
            display_name=f"SQLite database ({self._where()})",
            supports_incremental=False,
            supports_deletes=False,
            root_locator=",".join(sorted(self._selected)),
        )

    async def list_source_resources(
        self, request: ListSourceResources
    ) -> tuple[SourceResource, ...]:
        """Discover the tables an operator may approve for agent retrieval."""
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
        """Approve tables, and drop the cached ontology so the bound set is rebuilt."""
        available = {
            resource.resource_id
            for resource in await self.list_source_resources(
                ListSourceResources(connection_id=request.connection_id)
            )
        }
        if not set(request.resource_ids).issubset(available):
            raise SourceError(SourceFailureCode.NOT_FOUND, "selected SQLite table is unavailable")
        self._selected = set(request.resource_ids)
        self._ontology = None

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        """Datastores stay live; a sync only establishes the mapping checkpoint.

        The checkpoint is the file's own fingerprint, so "has this database
        changed" is answerable without reading a row of it.
        """
        await self._ensure_local()
        return SyncSourcePage(next_checkpoint=self._fingerprint or "live", has_more=False)

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        """Rows never travel through the document/blob fetch path."""
        del request
        raise SourceError(SourceFailureCode.UNSUPPORTED_CONTENT, "SQLite is queried live")

    async def close_source(self) -> None:
        """Drop the local copy when the granted connection goes away."""
        cache, self._cache_dir = self._cache_dir, None
        self._local = None
        self._fingerprint = ""
        self._schema_fingerprint = ""
        self._ontology = None
        if cache is not None:
            await asyncio.to_thread(shutil.rmtree, cache, True)

    # --- the datastore port -------------------------------------------------

    async def datastore_port(self) -> SQLiteAttachment:
        """Return this structural ``DatastorePort`` once its schema is bounded."""
        await self.introspect()
        return self

    async def introspect(self) -> Any:
        """The schema as the operator describes it, narrowed to approved tables.

        Approval is applied BEFORE the semantic layer, so a description of a
        table nobody approved cannot put that table back in front of an agent.
        """
        await self._ensure_local()
        self._invalidate_if_moved(self._fingerprint)
        if self._ontology is not None:
            return self._ontology
        raw = await asyncio.to_thread(self._introspect_blocking)
        if self._selected:
            from arcmemory.datastore import DatastoreOntology

            kept = {n: i for n, i in raw.tables.items() if n in self._selected}
            raw = DatastoreOntology(tables=kept, entity_map={})
        self._ontology = _overlay(self._connection_id, raw)
        return self._ontology

    async def persist_ontology(self, store: Any) -> None:
        """Write table SHAPE into memory — never a row, and never the path."""
        ontology = await self.introspect()
        layer = _layer_for(self._connection_id)
        for name, info in ontology.tables.items():
            slug = f"db-table-{name}"
            store.write_fact(
                slug, "primary_key", info.primary_key or "", name=name, entity_type="db_table"
            )
            store.write_fact(
                slug,
                "searchable_columns",
                ", ".join(info.searchable_columns),
                entity_type="db_table",
            )
            meaning = layer.table.get(name)
            if meaning is not None and meaning.entity:
                store.write_fact(slug, "entity", meaning.entity, entity_type="db_table")
            if meaning is not None and meaning.description:
                store.write_fact(slug, "description", meaning.description, entity_type="db_table")
            for column, referenced in info.foreign_keys.items():
                store.write_fact(slug, f"fk:{column}", referenced, entity_type="db_table")

    async def query(self, op: str, table: str, args: dict[str, object]) -> object:
        """Run one allowlisted, parameterized read against an approved table.

        ``introspect`` re-resolves the file first, so a remote database that has
        changed since the last read is re-fetched before the query rather than
        answered from a stale copy. That costs one ``stat`` per query and buys
        an agent that is never confidently wrong about live data.
        """
        ontology = await self.introspect()
        if table not in ontology.tables:
            raise ValueError(f"unknown or unapproved SQLite table: {table!r}")
        return await asyncio.to_thread(self._query_blocking, op, table, args)

    # --- resolving the file -------------------------------------------------

    async def _ensure_local(self) -> Path:
        """The path to a local, readable copy of the database.

        For a local connection that is the file itself. For a remote one it is a
        copy in this process's own private directory, refreshed only when the
        remote fingerprint has moved — so a question asked twice does not copy
        the database twice.
        """
        async with self._lock:
            if not self._host:
                return self._resolve_local_file()
            fingerprint = await self._remote_fingerprint()
            if self._local is not None and fingerprint == self._fingerprint:
                return self._local
            self._local = await self._fetch_remote()
            self._fingerprint = fingerprint
            return self._local

    def _resolve_local_file(self) -> Path:
        """Validate a same-machine path, refusing anything that is not a real file.

        A symlink is refused rather than followed: the path is operator-supplied
        configuration, but the thing it points at can be replaced by anyone who
        can write that directory, which is a different and much larger set of
        people.
        """
        path = Path(self._remote_path).expanduser()
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise SourceError(
                SourceFailureCode.NOT_FOUND, f"no SQLite file at {self._remote_path}"
            ) from exc
        if path.is_symlink() or not resolved.is_file():
            raise SourceError(
                SourceFailureCode.NOT_FOUND,
                f"{self._remote_path} is not a regular file",
            )
        self._local = resolved
        self._fingerprint = _stat_fingerprint(resolved)
        return resolved

    async def _remote_fingerprint(self) -> str:
        """``inode:mtime:size`` from the remote host — the cheapest "has it changed"."""
        out = await self._ssh(f"stat -c '%i:%Y:%s' {shlex.quote(self._remote_path)}")
        fingerprint = out.decode("utf-8", "replace").strip()
        if not fingerprint:
            raise SourceError(
                SourceFailureCode.NOT_FOUND,
                f"no SQLite file at {self._remote_path} on {self._host}",
            )
        size = fingerprint.rsplit(":", 1)[-1]
        if size.isdigit() and int(size) > _MAX_REMOTE_BYTES:
            raise SourceError(
                SourceFailureCode.TOO_LARGE,
                f"the database on {self._host} is {int(size) // (1024 * 1024)}MB, "
                f"over the {_MAX_REMOTE_BYTES // (1024 * 1024)}MB copy limit",
            )
        return fingerprint

    async def _fetch_remote(self) -> Path:
        """Copy the remote database into this process's own private directory.

        Private and per-process on purpose. A predictable path in a shared temp
        directory is a file another user can replace between the write and the
        open, and what would be opened is a database this agent then trusts.
        """
        if self._cache_dir is None:
            self._cache_dir = Path(tempfile.mkdtemp(prefix="arc-sqlite-"))
        target = self._cache_dir / "database.sqlite"
        payload = await self._ssh(f"cat {shlex.quote(self._remote_path)}")
        if not payload:
            raise SourceError(
                SourceFailureCode.NOT_FOUND,
                f"{self._remote_path} on {self._host} is empty or unreadable",
            )
        await asyncio.to_thread(_write_private, target, payload)
        return target

    async def _ssh(self, remote_command: str) -> bytes:
        """Run one read-only command on the host, with no shell on this side.

        ``ssh`` joins its trailing arguments and hands them to the REMOTE shell,
        so the path is quoted for that shell — the only place a quoting mistake
        here could turn a filename into a command.
        """
        try:
            process = await asyncio.create_subprocess_exec(
                "ssh",
                *_SSH_OPTIONS,
                self._host,
                remote_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            raise SourceError(
                SourceFailureCode.TRANSIENT, f"could not start ssh: {type(exc).__name__}"
            ) from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=_SSH_TIMEOUT_SECONDS
            )
        except TimeoutError as exc:
            process.kill()
            raise SourceError(
                SourceFailureCode.TRANSIENT, f"{self._host} did not answer within the timeout"
            ) from exc
        if process.returncode != 0:
            detail = stderr.decode("utf-8", "replace").strip()[:200] or "ssh failed"
            raise SourceError(SourceFailureCode.AUTH_REQUIRED, f"{self._host}: {detail}")
        return stdout

    def _introspect_blocking(self) -> Any:
        from arcmemory.datastore import SqliteDatastore

        with self._connection() as conn:
            return SqliteDatastore(conn).introspect()

    def _query_blocking(self, op: str, table: str, args: dict[str, object]) -> object:
        from arcmemory.datastore import SqliteDatastore

        with self._connection() as conn:
            return SqliteDatastore(conn).query(op, table, args)

    def _connection(self) -> Any:
        """A read-only connection, opened per call and closed with the block.

        Read-only is enforced by SQLite itself through ``mode=ro`` rather than by
        this module declining to write: a connector that can only read is a
        smaller promise to keep than one that merely intends to.
        """
        from contextlib import closing

        if self._local is None:
            raise SourceError(SourceFailureCode.NOT_FOUND, "the SQLite file is not open")
        return closing(sqlite3.connect(f"{self._local.as_uri()}?mode=ro", uri=True))

    def _invalidate_if_moved(self, fingerprint: str) -> None:
        """Drop the cached schema when the file itself has changed underneath it."""
        if fingerprint and fingerprint != self._schema_fingerprint:
            self._ontology = None
            self._schema_fingerprint = fingerprint

    async def _describe_schema(self) -> str:
        from arcmemory.semantic_layer import describe

        ontology = await self.introspect()
        return describe(ontology, _layer_for(self._connection_id))

    def _where(self) -> str:
        """How to name this database to a person, without leaking a credential."""
        return f"{self._host}:{self._remote_path}" if self._host else self._remote_path


def _overlay(connection_id: str, ontology: Any) -> Any:
    from arcmemory.semantic_layer import overlay

    return overlay(connection_id, ontology)


def _layer_for(connection_id: str) -> Any:
    from arcmemory.semantic_layer import layer_for

    return layer_for(connection_id)


def _write_private(target: Path, payload: bytes) -> None:
    """Write the copy so only this user can read it, replacing any earlier one."""
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)


def _stat_fingerprint(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_ino}:{stat.st_mtime_ns}:{stat.st_size}"


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


def _limit(value: object) -> int:
    try:
        return max(1, min(int(str(value)), _MAX_LIMIT))
    except ValueError as exc:
        raise ValueError("limit must be an integer") from exc


def build_native_attachment(context: dict[str, Any]) -> SQLiteAttachment:
    """Build from Arc's ephemeral reveal context; nothing here is persisted."""
    return SQLiteAttachment(
        str(context.get("database_path", "")),
        str(context.get("host", "")),
        str(context.get("connection_id", "")),
    )


__all__ = ["SQLiteAttachment", "build_native_attachment"]
