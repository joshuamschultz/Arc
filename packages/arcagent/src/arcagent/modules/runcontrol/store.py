"""Thin client that opens the arcstore-backed cancel-request directory.

No logic duplicated here — ``arcstore.cancellations.CancelStore`` owns the durable
directory model and race-safe resolve. This module only resolves the shared SQLite
path — the SAME db arcui and ``arc stop`` write to — and hands back an open store so
the agent watcher and the operator surfaces always agree on cancel state.
"""

from __future__ import annotations

from arcstore.backends.sqlite import SqliteBackend
from arcstore.cancellations import CancelStore
from arcstore.config import store_db_path


async def open_store(data_dir: str) -> CancelStore:
    """Open the arcstore ``cancellations`` collection against the shared arcui.db."""
    backend = SqliteBackend(store_db_path(data_dir or None))
    await backend.start()
    return CancelStore(backend)


__all__ = ["open_store"]
