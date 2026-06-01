"""StorageBackend Protocol — the one seam between the store and its backend.

Async, ``@runtime_checkable``, matching the ``arcteam.storage`` house style
(research §11.3). The transaction is an *implementation detail* of the backend
(the SQLite ingest commits a batch + cursor in one ``BEGIN IMMEDIATE``); it is
deliberately **not** on the Protocol — exposing ``begin()`` would force the
in-memory fake to fake it (research §11.3 supersedes the SDD §5.1 ``begin()``).

A second, in-memory implementation (``FakeBackend``) runs the same conformance
suite, proving no SQLite type leaks into this contract (FR-3 AC-3.4).
"""

from __future__ import annotations

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
class StorageBackend(Protocol):
    """Swappable storage abstraction for operational + audit data.

    All methods are async so a single contract spans the stdlib-``sqlite3``
    default (bridged via ``asyncio.to_thread``) and future network backends
    (Postgres/cloud) without changing producer or UI-read code.
    """

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
        order_by: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return rows as plain dicts (no driver cursors leak)."""
        ...

    async def get_cursor(self, name: str) -> int:
        """Return the persisted byte offset for a source file (0 if unknown)."""
        ...

    async def set_cursor(self, name: str, value: int) -> None:
        """Persist the byte offset consumed for a source file."""
        ...
