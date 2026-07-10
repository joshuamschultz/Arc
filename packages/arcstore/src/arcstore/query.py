"""Read API consumed by arcui / arctui (FR-3, FR-5).

Thin, backend-agnostic convenience reads over the ``StorageBackend.query``
seam. The UI never touches a backend driver — it calls these functions, which
return plain dicts. Cursor-incremental fetch (``after_seq``) keeps poll
payloads O(new rows) rather than O(history) (research §11.6).
"""

from __future__ import annotations

from typing import Any

from arcstore.backends.base import (
    AUDIT_TABLE,
    SKILL_BODIES_TABLE,
    SKILL_CANDIDATES_TABLE,
    StorageBackend,
    table_for_kind,
)

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


async def skill_versions(
    backend: StorageBackend,
    skill_name: str,
    *,
    limit: int = _DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Version timeline for a skill — metadata only, ordered by generation.

    Rows are content-keyed, so a manifest change (score update, rollback
    flipping ``active``) inserts a new row version; the latest row per
    ``candidate_id`` wins here. ``body_hash`` None marks a pending/pruned body.
    Bodies never ride this payload — fetch them via :func:`skill_candidate_body`.
    """
    rows = await backend.query(
        SKILL_CANDIDATES_TABLE, where={"skill_name": skill_name}, order_by="ts"
    )
    latest: dict[str, dict[str, Any]] = {row["candidate_id"]: row for row in rows}
    ordered = sorted(latest.values(), key=lambda r: (r.get("generation") or 0, r.get("ts") or ""))
    return ordered[:limit]


async def skill_candidate_body(
    backend: StorageBackend,
    skill_name: str,
    candidate_id: str,
) -> str | None:
    """Full candidate text by id (``None`` when the body is pending/pruned)."""
    versions = await skill_versions(backend, skill_name)
    body_hash = next((v["body_hash"] for v in versions if v["candidate_id"] == candidate_id), None)
    if not body_hash:
        return None
    rows = await backend.query(SKILL_BODIES_TABLE, where={"record_id": body_hash}, limit=1)
    return str(rows[0]["body"]) if rows else None
