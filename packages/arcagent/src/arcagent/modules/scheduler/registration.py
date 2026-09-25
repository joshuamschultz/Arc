"""Authorize one immutable schedule definition before publishing its local projection."""

from __future__ import annotations

import hashlib

from arcagent.core.control_contract import (
    ControlActionProofSource,
    ControlArtifactAuthority,
    ControlArtifactRefusedError,
    ControlArtifactUnavailableError,
)
from arcagent.modules.scheduler.models import ScheduleEntry
from arcagent.modules.scheduler.occurrence import canonical_definition


async def register_schedule_revision(
    entry: ScheduleEntry,
    *,
    previous: ScheduleEntry | None,
    tenant_id: str,
    agent_did: str,
    authority: ControlArtifactAuthority,
    actor_proof_source: ControlActionProofSource,
) -> ScheduleEntry:
    """Anchor signed broker approval for a validated candidate definition."""
    prior_approval = None if previous is None else previous.approval
    if previous is not None and (previous.id != entry.id or prior_approval is None):
        raise ControlArtifactRefusedError("schedule prior revision is missing or mismatched")
    definition = canonical_definition(entry)
    try:
        actor_proof = await actor_proof_source("schedule", entry.id, definition)
        if not isinstance(actor_proof, bytes) or not actor_proof:
            raise ControlArtifactRefusedError("schedule actor proof is absent")
        approval = await authority.register_revision(
            tenant_id=tenant_id,
            agent_did=agent_did,
            purpose="schedule",
            artifact_id=entry.id,
            canonical_definition=definition,
            expected_revision=None if prior_approval is None else prior_approval.revision,
            actor_proof=actor_proof,
        )
    except ControlArtifactRefusedError:
        raise
    except Exception as exc:
        raise ControlArtifactUnavailableError(
            "schedule registration authority unavailable"
        ) from exc
    if (
        approval.tenant_id != tenant_id
        or approval.agent_did != agent_did
        or approval.purpose != "schedule"
        or approval.artifact_id != entry.id
        or approval.definition_digest != hashlib.sha256(definition).hexdigest()
        or approval.revision != (1 if prior_approval is None else prior_approval.revision + 1)
        or approval.revoked
    ):
        raise ControlArtifactRefusedError(
            "schedule approval facts differ from submitted definition"
        )
    return entry.model_copy(update={"approval": approval})


__all__ = ["register_schedule_revision"]
