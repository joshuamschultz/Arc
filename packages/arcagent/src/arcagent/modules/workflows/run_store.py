"""Thin client that opens the arcstore-backed workflow Run directory (SPEC-061).

No logic duplicated here — ``arcstore.runs.RunStore`` owns the Run aggregate.
This mirrors ``modules/tasks/store.py`` exactly: resolve the SAME shared SQLite
path the dashboard reads, and hand back an open store, so the agent surface and
every other reader agree on run state.
"""

from __future__ import annotations

from arcstore.backends.sqlite import SqliteBackend
from arcstore.config import store_db_path
from arcstore.runs import RunStore


async def open_run_store(data_dir: str) -> RunStore:
    """Open the arcstore ``runs`` collection against the shared arcui.db."""
    backend = SqliteBackend(store_db_path(data_dir or None))
    await backend.start()
    return RunStore(backend)


__all__ = ["open_run_store"]
