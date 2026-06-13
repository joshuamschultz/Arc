"""Read API consumed by arcui / arctui (FR-3, FR-5).

Thin, backend-agnostic convenience reads over the ``StorageBackend.query``
seam. The UI never touches a backend driver — it calls these functions, which
return plain dicts. Cursor-incremental fetch (``after_seq``) keeps poll
payloads O(new rows) rather than O(history) (research §11.6).
"""

from __future__ import annotations

from typing import Any

from arcstore.backends.base import AUDIT_TABLE, StorageBackend, table_for_kind

_DEFAULT_LIMIT = 100


async def recent(
    backend: StorageBackend,
    kind: str,
    *,
    actor_did: str | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Most-recent operational records of ``kind`` (newest first)."""
    where = {"actor_did": actor_did} if actor_did else None
    return await backend.query(table_for_kind(kind), where=where, order_by="ts DESC", limit=limit)


async def audit_records(
    backend: StorageBackend,
    *,
    limit: int = _DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Most-recent mirrored audit-chain records (by sequence, newest first)."""
    return await backend.query(AUDIT_TABLE, order_by="seq DESC", limit=limit)
