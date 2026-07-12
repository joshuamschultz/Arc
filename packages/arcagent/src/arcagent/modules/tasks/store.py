"""Thin client that opens the arcstore-backed Task directory (SPEC-056 Phase B).

No logic duplicated here — ``arcstore.tasks.TaskStore`` owns the durable
directory model, atomic claim/assign, and dependency gating (SPEC-056 Phase
A). This module only resolves the shared SQLite path — the SAME db arcui
reads (``arcui.observe.Observe``, SDD §2/§6) — and hands back an open store
so the agent and the dashboard always agree on task state.
"""

from __future__ import annotations

from arcstore.backends.sqlite import SqliteBackend
from arcstore.config import resolve_data_dir
from arcstore.tasks import TaskStore


async def open_store(data_dir: str) -> TaskStore:
    """Open the arcstore ``tasks`` collection against the shared arcui.db."""
    base = resolve_data_dir(data_dir or None)
    backend = SqliteBackend(base / "store" / "arcui.db")
    await backend.start()
    return TaskStore(backend)


__all__ = ["open_store"]
