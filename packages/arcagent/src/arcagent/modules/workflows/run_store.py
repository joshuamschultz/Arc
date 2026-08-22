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

from collections.abc import Awaitable, Callable

from arcstore.backends import ArcStoreBackend, open_backend
from arcteam.workflow.stores import WorkflowRunStore


async def open_run_store(
    *, opener: Callable[[], Awaitable[ArcStoreBackend]] | None = None
) -> tuple[WorkflowRunStore, ArcStoreBackend]:
    """Open the configured workflow run plane and return its owner."""
    backend = await opener() if opener is not None else open_backend()
    if opener is None:
        await backend.start()
    return WorkflowRunStore(backend), backend


__all__ = ["open_run_store"]
