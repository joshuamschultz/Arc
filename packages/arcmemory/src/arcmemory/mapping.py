"""Source-to-home mapping proposal, SPEC-035 approval gate, and mapping-as-facts.

A connected source's routing (which memory "homes" -- memory / document /
datastore -- it feeds) is not something the agent may commit unilaterally: it
is staged as a SPEC-035 :class:`~arcstore.approvals.PendingApproval` row and
stays un-committable until an operator resolves it ``approved`` (fail-closed
otherwise). Once approved, the mapping is persisted as ordinary facts on a
per-source ``mapping`` :class:`~arcmemory.types.Entity` -- the same additive
``was:`` trail every other fact gets, so a changed mapping is never a
destructive overwrite (SemanticStore.write_fact).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from arcstore.approvals import ApprovalStore, PendingApproval

from arcmemory.security import content_hash
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import SourceMapping

_TOOL = "memory.map_source"


def mapping_call_hash(
    source_id: str,
    homes: list[str],
    *,
    revision: str = "",
    content_hash_value: str = "",
) -> str:
    """Hash the complete mapping proposal used as the approval call binding."""
    return content_hash(
        "\0".join((source_id, ",".join(sorted(homes)), revision, content_hash_value))
    )


async def stage_mapping_proposal(
    proposal: SourceMapping,
    *,
    approval_store: ApprovalStore,
    agent_did: str,
    agent_label: str = "",
    expires_in_seconds: float = 86_400.0,
) -> str:
    """Write a SPEC-035 pending row for ``proposal``. Returns the pending row id.

    Ingest must not commit the mapping (:func:`commit_mapping`) until this row
    is resolved ``approved`` -- see :func:`approved_mapping`.
    """
    await approval_store.start()
    call_hash = mapping_call_hash(
        proposal.source_id,
        proposal.homes,
        revision=proposal.revision,
        content_hash_value=proposal.content_hash,
    )
    for existing in await approval_store.list():
        if existing.call_hash != call_hash:
            continue
        if (
            existing.arguments.get("revision", "") == proposal.revision
            and existing.arguments.get("content_hash", "") == proposal.content_hash
        ):
            return existing.id
    pending = PendingApproval(
        id=str(uuid4()),
        agent_did=agent_did,
        agent_label=agent_label,
        tool=_TOOL,
        legs=[],
        call_hash=call_hash,
        arguments={
            "source_id": proposal.source_id,
            "homes": ",".join(proposal.homes),
            "revision": proposal.revision,
            "content_hash": proposal.content_hash,
        },
        provenance=[],
        expires_at=(datetime.now(UTC) + timedelta(seconds=expires_in_seconds)).isoformat(),
    )
    created = await approval_store.create(pending)
    return created.id


async def approved_mapping(
    source_id: str,
    homes: list[str],
    *,
    approval_store: ApprovalStore,
    revision: str = "",
    content_hash_value: str = "",
) -> bool:
    """True iff a pending row with this call_hash exists AND is ``approved``.

    Fail-closed: pending, denied, expired, or never-staged all return False.
    """
    await approval_store.start()
    target = mapping_call_hash(
        source_id, homes, revision=revision, content_hash_value=content_hash_value
    )
    approved = await approval_store.list(status="approved")
    now = datetime.now(UTC)
    return any(
        row.call_hash == target
        and (row.expires_at is None or datetime.fromisoformat(row.expires_at) > now)
        for row in approved
    )


def commit_mapping(mapping: SourceMapping, *, store: SemanticStore) -> None:
    """Persist ``mapping`` as facts on its per-source ``mapping`` Entity.

    A re-commit with a CHANGED ``homes`` set is a normal fact write, so
    ``write_fact`` folds the prior value into an additive ``was:`` trail --
    never a destructive overwrite.
    """
    store.write_fact(
        f"mapping-{mapping.source_id}",
        "homes",
        ",".join(mapping.homes),
        entity_type="mapping",
    )
    if mapping.revision:
        store.write_fact(
            f"mapping-{mapping.source_id}", "revision", mapping.revision, entity_type="mapping"
        )
    if mapping.content_hash:
        store.write_fact(
            f"mapping-{mapping.source_id}",
            "content_hash",
            mapping.content_hash,
            entity_type="mapping",
        )


def load_committed_mapping(source_id: str, *, store: SemanticStore) -> SourceMapping | None:
    """Read the mapping Entity's current ``homes`` fact -> :class:`SourceMapping`.

    None when the source was never committed.
    """
    entity = store.read(f"mapping-{source_id}")
    if entity is None:
        return None
    fact = next((f for f in entity.facts if f.predicate == "homes"), None)
    if fact is None:
        return None
    homes = [home for home in fact.value.split(",") if home]
    values = {item.predicate: item.value for item in entity.facts}
    return SourceMapping(
        source_id=source_id,
        homes=homes,
        revision=values.get("revision", ""),
        content_hash=values.get("content_hash", ""),
    )


__all__ = [
    "approved_mapping",
    "commit_mapping",
    "load_committed_mapping",
    "mapping_call_hash",
    "stage_mapping_proposal",
]
