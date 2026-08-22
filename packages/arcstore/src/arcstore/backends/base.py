"""ArcStore's complete asynchronous persistence contract."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

# Operational tables — one per SpoolRecord.kind, all sharing the flat columns.
OPERATIONAL_TABLES: tuple[str, ...] = (
    "llm_calls",
    "run_events",
    "agent_events",
    "tool_events",
    "spawn_events",
)

# The arctrust WORM mirror (with a per-row ``verified`` flag set on ingest).
AUDIT_TABLE = "audit_chain"

# The arcskill candidate-store mirror (SPEC-054 REQ-120): version metadata rows
# keyed by content hash, and candidate bodies keyed by their sha256 body hash.
SKILL_CANDIDATES_TABLE = "skill_candidates"
SKILL_BODIES_TABLE = "skill_candidate_bodies"

# The mutable directory plane (SPEC-056 Phase 0A / SPEC-032 slice): unlike
# OPERATIONAL_TABLES (insert-once spool), rows here are overwritten in place —
# one collection per directory entity (tasks, entities, teams, channels, ...).
MUTABLE_RECORDS_TABLE = "mutable_records"
APPROVAL_OUTBOX_TABLE = "approval_outbox"

STORE_TABLES = frozenset(
    (
        *OPERATIONAL_TABLES,
        AUDIT_TABLE,
        SKILL_CANDIDATES_TABLE,
        SKILL_BODIES_TABLE,
        APPROVAL_OUTBOX_TABLE,
    )
)

_KIND_TABLE = {
    "llm_call": "llm_calls",
    "run_event": "run_events",
    "agent_event": "agent_events",
    "tool_event": "tool_events",
    "spawn_event": "spawn_events",
}


def table_for_kind(kind: str) -> str:
    """Map a ``SpoolRecord.kind`` to its operational table name."""
    try:
        return _KIND_TABLE[kind]
    except KeyError as exc:
        raise ValueError(f"unknown spool kind: {kind!r}") from exc


@runtime_checkable
class ArcStoreBackend(Protocol):
    """One driver-neutral contract for ArcStore's PostgreSQL data plane."""

    async def start(self) -> None:
        """Open resources / create schema. Idempotent."""
        ...

    async def stop(self) -> None:
        """Release resources. Idempotent."""
        ...

    async def upsert(self, table: str, key: str, row: dict[str, Any]) -> None:
        """Insert ``row`` under ``key``; a repeated ``key`` is a no-op (idempotent)."""
        ...

    async def upsert_many(self, table: str, rows: list[tuple[str, dict[str, Any]]]) -> None:
        """Batch form of :meth:`upsert` — ``(key, row)`` pairs, idempotent per key."""
        ...

    async def query(
        self,
        table: str,
        *,
        where: dict[str, Any] | None = None,
        ts_gte: str | None = None,
        order_by: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return rows as plain dicts (no driver cursors leak).

        ``ts_gte`` adds a ``ts >= ?`` lower bound (ISO-8601 lexicographic
        compare), letting a time window filter push down to the store so the
        ``limit`` applies after the cutoff instead of over the whole table.
        """
        ...

    async def get_cursor(self, name: str) -> int:
        """Return the persisted byte offset for a source file (0 if unknown)."""
        ...

    async def set_cursor(self, name: str, value: int) -> None:
        """Persist the byte offset consumed for a source file."""
        ...

    async def mutable_write(
        self,
        collection: str,
        key: str,
        value: dict[str, Any],
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> None: ...

    async def mutable_read(self, collection: str, key: str) -> dict[str, Any] | None: ...

    async def mutable_delete(
        self,
        collection: str,
        key: str,
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> bool: ...

    async def mutable_query(
        self,
        collection: str,
        *,
        where: dict[str, Any] | None = None,
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
    ) -> bool: ...

    async def mutable_create_batch(
        self,
        collection: str,
        entries: Sequence[tuple[str, dict[str, Any]]],
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> list[dict[str, Any]]: ...

    async def mutable_increment(
        self,
        collection: str,
        key: str,
        deltas: dict[str, int | float],
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> bool: ...

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
    ) -> bool: ...

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
    ) -> bool: ...

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
    ) -> bool: ...

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
    ) -> None: ...

    async def claim_outbox(
        self, consumer_id: str, *, limit: int = 100
    ) -> list[dict[str, Any]]: ...

    async def ack_outbox(self, consumer_id: str, event_ids: Sequence[str]) -> None: ...

    async def nack_outbox(
        self,
        consumer_id: str,
        event_id: str,
        *,
        retry_after_seconds: float,
    ) -> bool: ...


StorageBackend = ArcStoreBackend
