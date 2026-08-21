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

from uuid import uuid4

from arcstore.approvals import ApprovalStore, PendingApproval

from arcmemory.security import content_hash
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import SourceMapping

_TOOL = "memory.map_source"


def mapping_call_hash(source_id: str, homes: list[str]) -> str:
    """Content hash over ``source_id`` + sorted ``homes`` -- the approval's call_hash."""
    return content_hash(f"{source_id}:{','.join(sorted(homes))}")


async def stage_mapping_proposal(
    proposal: SourceMapping,
    *,
    approval_store: ApprovalStore,
    agent_did: str,
    agent_label: str = "",
) -> str:
    """Write a SPEC-035 pending row for ``proposal``. Returns the pending row id.

    Ingest must not commit the mapping (:func:`commit_mapping`) until this row
    is resolved ``approved`` -- see :func:`approved_mapping`.
    """
    await approval_store.start()
    pending = PendingApproval(
        id=str(uuid4()),
        agent_did=agent_did,
        agent_label=agent_label,
        tool=_TOOL,
        legs=[],
        call_hash=mapping_call_hash(proposal.source_id, proposal.homes),
        arguments={"source_id": proposal.source_id, "homes": ",".join(proposal.homes)},
        provenance=[],
    )
    created = await approval_store.create(pending)
    return created.id


async def approved_mapping(
    source_id: str, homes: list[str], *, approval_store: ApprovalStore
) -> bool:
    """True iff a pending row with this call_hash exists AND is ``approved``.

    Fail-closed: pending, denied, expired, or never-staged all return False.
    """
    await approval_store.start()
    target = mapping_call_hash(source_id, homes)
    approved = await approval_store.list(status="approved")
    return any(row.call_hash == target for row in approved)


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
    return SourceMapping(source_id=source_id, homes=homes)


__all__ = [
    "approved_mapping",
    "commit_mapping",
    "load_committed_mapping",
    "mapping_call_hash",
    "stage_mapping_proposal",
]
