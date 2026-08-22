"""Thin client that opens the arcstore-backed cancel-request directory.

No logic duplicated here — ``arcstore.cancellations.CancelStore`` owns the durable
directory model and race-safe resolve. This module only resolves the shared SQLite
path — the SAME db arcui and ``arc stop`` write to — and hands back an open store so
the agent watcher and the operator surfaces always agree on cancel state.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from arcstore.backends import ArcStoreBackend, open_backend
from arcstore.cancellations import CancelStore


async def open_store(
    *, opener: Callable[[], Awaitable[ArcStoreBackend]] | None = None
) -> tuple[CancelStore, ArcStoreBackend]:
    """Open the configured cancellations collection and return its owner."""
    backend = await opener() if opener is not None else open_backend()
    if opener is None:
        await backend.start()
    return CancelStore(backend), backend


__all__ = ["open_store"]
