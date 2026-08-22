"""Thin opener for the arcstore-backed pending-approval directory (SPEC-035).

Mirrors ``modules/tasks/store.open_store``: no logic here — ``arcstore.approvals.
ApprovalStore`` owns the durable model and race-safe resolve. This only resolves
the shared SQLite path (the SAME arcui.db arcui and ``arc approve`` read) so the
agent, the dashboard, and the CLI always agree on which requests are pending.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from arcstore.approvals import ApprovalStore
from arcstore.backends import ArcStoreBackend, open_backend
from arcstore.config import ArcStoreConfig
from pydantic import SecretStr


async def open_approval_store(
    *,
    opener: Callable[[], Awaitable[ArcStoreBackend]] | None = None,
    config: ArcStoreConfig | None = None,
    secret: SecretStr | str | None = None,
) -> tuple[ApprovalStore, ArcStoreBackend]:
    """Open the configured approvals collection and return its owner."""
    backend = (
        await opener()
        if opener is not None
        else open_backend(
            config=config, secret=SecretStr(secret) if isinstance(secret, str) else secret
        )
    )
    if opener is None:
        await backend.start()
    return ApprovalStore(backend), backend


__all__ = ["open_approval_store"]
