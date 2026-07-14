"""Thin opener for the arcstore-backed pending-approval directory (SPEC-035).

Mirrors ``modules/tasks/store.open_store``: no logic here — ``arcstore.approvals.
ApprovalStore`` owns the durable model and race-safe resolve. This only resolves
the shared SQLite path (the SAME arcui.db arcui and ``arc approve`` read) so the
agent, the dashboard, and the CLI always agree on which requests are pending.
"""

from __future__ import annotations

from arcstore.approvals import ApprovalStore
from arcstore.backends.sqlite import SqliteBackend
from arcstore.config import store_db_path


async def open_approval_store(data_dir: str = "") -> ApprovalStore:
    """Open the arcstore ``approvals`` collection against the shared arcui.db."""
    backend = SqliteBackend(store_db_path(data_dir or None))
    await backend.start()
    return ApprovalStore(backend)


__all__ = ["open_approval_store"]
