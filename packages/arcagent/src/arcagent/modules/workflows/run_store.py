"""Thin client that opens the arcstore-backed workflow Run directory (SPEC-061).

No logic duplicated here — ``arcteam.workflow.stores.WorkflowRunStore`` owns the
run plane and ``arcstore.runs.RunStore`` owns the Run aggregate underneath it.
This mirrors ``modules/tasks/store.py`` exactly: resolve the SAME shared SQLite
path the dashboard reads, and hand back an open store, so the agent surface and
every other reader agree on run state.

The arcteam store, not the arcstore one: the control plane's purge guard asks it
for a run COUNT and the read tools ask it for a workflow's runs, and neither
method exists on the raw aggregate store — handing that one over left purge
refusing every call and the run-history tools reporting no runner.
"""

from __future__ import annotations

from arcstore.backends.sqlite import SqliteBackend
from arcstore.config import store_db_path
from arcteam.workflow.stores import WorkflowRunStore


async def open_run_store(data_dir: str) -> WorkflowRunStore:
    """Open the workflow run plane against the shared arcui.db."""
    backend = SqliteBackend(store_db_path(data_dir or None))
    await backend.start()
    return WorkflowRunStore(backend)


__all__ = ["open_run_store"]
