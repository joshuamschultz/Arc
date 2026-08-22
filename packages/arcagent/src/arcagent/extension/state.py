"""ConnectionStateStore — per-connection operational state (SPEC-062 COMP-019).

Health, last successful use, credential *coordinates*, approved tool-contract
hashes, and dependency declarations for one connection — the state REQ-295 lets a
management surface report without probing the external service, and the
approved-hash side REQ-291's rug-pull defence reads and writes.

Keyed by the connection alone, because that is what the state is about. The
approved tool contract is what the upstream serves, not what one agent sees, so
two agents granted the same account share one approval: an operator approves a
changed contract once rather than once per grantee, and a suspension cannot be
in force for one agent and cleared for another.

This is operational data *about* a connection, not the agent's own brain: it is
shared with the CLI, the TUI, and the dashboard, so it belongs on the arcstore
mutable plane (the operational data plane, ``.claude/steering/structure.md``)
next to tasks, runs, and approvals — not in the agent workspace, where ADR-029
puts memory, sessions, identity, and the audit chain.

Two invariants earn their own code:

* **No credential values, ever.** The schema records the store coordinates an
  auditor needs (expiry, issuer, audience) and has no field able to hold a
  secret; ``extra="forbid"`` means a caller trying to stash one raises instead
  of quietly persisting it (REQ-265, REQ-277). Values live in COMP-010.
* **Never a half-written record.** Every mutation is a single atomic backend
  statement — a merge patch, not a read-modify-write — so a crash can only
  leave the prior row or the new one, and two writers touching different fields
  cannot lose each other's update. A row that is nonetheless unreadable is
  loud and *local*: :meth:`get` raises naming the connection, and :meth:`list`
  skips it with a logged error rather than letting one bad row take every
  other connection's status down with it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal, Protocol

from arctrust.audit import AuditSink
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from arcagent.core.errors import ExtensionError

CONNECTION_COLLECTION = "connections"

ConnectionHealth = Literal["unknown", "healthy", "degraded", "needs_attention"]

_logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ConnectionRecord(BaseModel):
    """Operational state of one connected account.

    Frozen — mutation always goes through :class:`ConnectionStateStore`, which
    patches the durable row; nothing holds a live record and edits it in place.
    ``extra="forbid"`` is load-bearing rather than tidiness: it is what stops a
    caller from smuggling a token into this store under an unmodeled key.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    connection: str
    health: ConnectionHealth = "unknown"
    last_success_at: str | None = None
    # Credential *coordinates* only — REQ-277 records the store, item, field,
    # caller, and outcome of a credential read, and never the value.
    credential_expires_at: str | None = None
    credential_issuer: str | None = None
    credential_audience: str | None = None
    # When the credential was last renewed. Distinct from ``updated_at``, which
    # any write moves — a health mark is not a refresh (COMP-011).
    credential_last_refresh_at: str | None = None
    # tool name -> hash of (name, description, input schema) as approved (REQ-291).
    approved_tool_hashes: dict[str, str] = Field(default_factory=dict)
    dependency_declarations: list[str] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None


class MutableConnectionBackend(Protocol):
    """The mutable-plane primitives this store needs (see ``arcstore.runs``).

    A local structural Protocol, not an import of a concrete backend: arcstore
    is an optional peer of arcagent, and the store itself must stay testable
    against any conforming plane.
    """

    async def mutable_create_batch(
        self,
        collection: str,
        entries: Sequence[tuple[str, dict[str, Any]]],
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> list[dict[str, Any]]: ...

    async def mutable_merge(
        self,
        collection: str,
        key: str,
        patch: dict[str, Any],
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> bool: ...

    async def mutable_read(self, collection: str, key: str) -> dict[str, Any] | None: ...

    async def mutable_query(
        self, collection: str, *, where: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]: ...

    async def mutable_delete(
        self, collection: str, key: str, *, actor_did: str, sink: Any | None = None
    ) -> bool: ...


class ConnectionStateStore:
    """Connection directory over the mutable plane's ``"connections"`` collection."""

    _COLLECTION: ClassVar[str] = CONNECTION_COLLECTION

    def __init__(
        self, backend: MutableConnectionBackend, *, sink: AuditSink | None = None
    ) -> None:
        self._backend = backend
        self._sink = sink

    async def close(self) -> None:
        """Release the backend owned by this state-store instance."""
        stop = getattr(self._backend, "stop", None)
        if stop is not None:
            await stop()

    async def create(self, record: ConnectionRecord, *, actor_did: str) -> ConnectionRecord:
        """Register a connection, returning the stored record.

        Insert-if-absent: re-running an install returns the connection already
        on disk rather than overwriting it, so a repeated ``add`` can never
        silently discard the tool contracts an operator already approved.
        """
        proposed = record.model_copy(update={"created_at": _now()})
        rows = await self._backend.mutable_create_batch(
            self._COLLECTION,
            [(record.connection, proposed.model_dump(mode="json"))],
            actor_did=actor_did,
            sink=self._sink,
        )
        return self._load(record.connection, rows[0])

    async def get(self, connection: str) -> ConnectionRecord | None:
        """Return one connection's state, or None if it was never registered."""
        raw = await self._backend.mutable_read(self._COLLECTION, connection)
        return self._load(connection, raw) if raw is not None else None

    async def list(self, *, health: ConnectionHealth | None = None) -> list[ConnectionRecord]:
        """Return every readable connection, optionally filtered.

        An unreadable row is logged with its key and skipped: a corrupt record
        for one connection must not blank out the status of every other one.
        """
        where: dict[str, Any] = {}
        if health is not None:
            where["health"] = health
        rows = await self._backend.mutable_query(self._COLLECTION, where=where)
        records: list[ConnectionRecord] = []
        for row in rows:
            key = str(row.get("connection", "?"))
            try:
                records.append(self._load(key, row))
            except ExtensionError as exc:
                _logger.error("skipping unreadable connection state row: %s", exc.message)
        return records

    async def mark_healthy(self, connection: str, *, actor_did: str) -> bool:
        """Record a successful use — health plus the time it happened (REQ-295)."""
        now = _now()
        return await self._patch(
            connection, {"health": "healthy", "last_success_at": now}, actor_did=actor_did
        )

    async def set_health(
        self, connection: str, health: ConnectionHealth, *, actor_did: str
    ) -> bool:
        """Set health without claiming a successful use (REQ-295, REQ-289)."""
        return await self._patch(connection, {"health": health}, actor_did=actor_did)

    async def record_credential_metadata(
        self,
        connection: str,
        *,
        expires_at: str | None = None,
        issuer: str | None = None,
        audience: str | None = None,
        last_refresh_at: str | None = None,
        actor_did: str,
    ) -> bool:
        """Record credential coordinates so renewal can run ahead of expiry (REQ-287).

        Coordinates only — the value belongs to the secret store (COMP-010) and
        is never handed to this one. Each argument left as None is omitted from
        the patch rather than written as null, so recording a refresh never
        erases an issuer the caller happened not to pass.
        """
        patch: dict[str, Any] = {}
        if expires_at is not None:
            patch["credential_expires_at"] = expires_at
        if issuer is not None:
            patch["credential_issuer"] = issuer
        if audience is not None:
            patch["credential_audience"] = audience
        if last_refresh_at is not None:
            patch["credential_last_refresh_at"] = last_refresh_at
        return await self._patch(connection, patch, actor_did=actor_did)

    async def approve_tool_contract(
        self, connection: str, tool: str, contract_hash: str, *, actor_did: str
    ) -> bool:
        """Record the hash of a tool contract as approved (REQ-291).

        The patch names only this tool, and the backend merges it recursively
        inside one statement, so two tools approved at the same instant both
        land — a read-modify-write of the whole map would lose one.
        """
        return await self._patch(
            connection, {"approved_tool_hashes": {tool: contract_hash}}, actor_did=actor_did
        )

    async def approved_hash(self, connection: str, tool: str) -> str | None:
        """The contract hash approved for ``tool``, or None if never approved (REQ-291)."""
        record = await self.get(connection)
        return record.approved_tool_hashes.get(tool) if record is not None else None

    async def declare_dependencies(
        self, connection: str, dependencies: Sequence[str], *, actor_did: str
    ) -> bool:
        """Record what this extension declares, so removal can reference-count it."""
        return await self._patch(
            connection, {"dependency_declarations": list(dependencies)}, actor_did=actor_did
        )

    async def forget(self, connection: str, *, actor_did: str) -> bool:
        """Drop a connection's state. Returns True if a record was removed."""
        return await self._backend.mutable_delete(
            self._COLLECTION, connection, actor_did=actor_did, sink=self._sink
        )

    async def _patch(self, connection: str, patch: dict[str, Any], *, actor_did: str) -> bool:
        """Merge ``patch`` into an existing row in one atomic statement.

        Returns False when the connection does not exist — an update never
        conjures a half-populated record out of a typo'd connection name.
        """
        if not patch:
            return False
        patch["updated_at"] = _now()
        return await self._backend.mutable_merge(
            self._COLLECTION,
            connection,
            patch,
            actor_did=actor_did,
            sink=self._sink,
        )

    def _load(self, key: str, row: dict[str, Any]) -> ConnectionRecord:
        try:
            return ConnectionRecord.model_validate(row)
        except ValidationError as exc:
            raise ExtensionError(
                code="CONNECTION_STATE_UNREADABLE",
                message=f"connection state row is unreadable: {key}",
                details={"connection": key, "errors": exc.error_count()},
            ) from exc


async def open_connection_state(
    *, opener: Callable[[], Awaitable[Any]] | None = None
) -> ConnectionStateStore:
    """Open the ``connections`` collection against the shared operational db.

    arcstore is imported here rather than at module scope: it is an optional
    peer of arcagent (the pattern ``core/agent.py`` already uses), so the store
    class stays importable — and unit-testable against any conforming plane —
    on a machine where the data plane is not installed.
    """
    if opener is not None:
        backend = await opener()
    else:
        from arcstore.backends import open_backend

        backend = open_backend()
        await backend.start()
    return ConnectionStateStore(backend)


__all__ = [
    "CONNECTION_COLLECTION",
    "ConnectionHealth",
    "ConnectionRecord",
    "ConnectionStateStore",
    "MutableConnectionBackend",
    "open_connection_state",
]
