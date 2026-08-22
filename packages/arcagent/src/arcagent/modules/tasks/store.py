"""Thin client that opens the arcstore-backed Task directory (SPEC-056 Phase B).

No logic duplicated here — ``arcstore.tasks.TaskStore`` owns the durable
directory model, atomic claim/assign, and dependency gating (SPEC-056 Phase
A). This module only resolves the shared SQLite path — the SAME db arcui
reads (``arcui.observe.Observe``, SDD §2/§6) — and hands back an open store
so the agent and the dashboard always agree on task state.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from arcstore.backends import ArcStoreBackend, open_backend
from arcstore.tasks import TaskStore


async def open_store(
    *, opener: Callable[[], Awaitable[ArcStoreBackend]] | None = None
) -> tuple[TaskStore, ArcStoreBackend]:
    """Open the configured tasks collection and return its backend owner."""
    backend = await opener() if opener is not None else open_backend()
    if opener is None:
        await backend.start()
    return TaskStore(backend), backend


__all__ = ["open_store"]
