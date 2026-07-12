"""Thin client that opens the arcstore-backed Task directory (SPEC-056 Phase B).

No logic duplicated here — ``arcstore.tasks.TaskStore`` owns the durable
directory model, atomic claim/assign, and dependency gating (SPEC-056 Phase
A). This module only resolves the shared SQLite path — the SAME db arcui
reads (``arcui.observe.Observe``, SDD §2/§6) — and hands back an open store
so the agent and the dashboard always agree on task state.
"""

from __future__ import annotations

from arcstore.backends.sqlite import SqliteBackend
from arcstore.config import store_db_path
from arcstore.tasks import TaskStore


async def open_store(data_dir: str) -> TaskStore:
    """Open the arcstore ``tasks`` collection against the shared arcui.db."""
    backend = SqliteBackend(store_db_path(data_dir or None))
    await backend.start()
    return TaskStore(backend)


__all__ = ["open_store"]
