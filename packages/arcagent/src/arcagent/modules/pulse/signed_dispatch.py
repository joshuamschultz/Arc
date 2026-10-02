"""Verify one pulse revision and due slot before accepting an agent run."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from arcagent.core.control_contract import (
    ControlArtifactAuthority,
    ControlArtifactRefusedError,
    ControlArtifactUnavailableError,
)
from arcagent.core.run_contract import (
    CanonicalRunRequest,
    RunOutcomeUnknownError,
    RunTriggerIssuer,
)
from arcagent.modules.pulse import PulseCheck


def canonical_definition(check: PulseCheck) -> bytes:
    """Return exact mutable pulse instructions covered by operator approval."""
    return json.dumps(
        {
            "name": check.name,
            "interval_minutes": check.interval_minutes,
            "action": check.action,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def pulse_occurrence(check: PulseCheck, due_at: datetime) -> tuple[str, str]:
    """Bind a due slot and check identity to one deterministic accepted run."""
    if due_at.tzinfo is None or due_at.utcoffset() is None:
        raise ValueError("pulse due time must be aware")
    slot = int(due_at.timestamp()) // (check.interval_minutes * 60)
    body = json.dumps(
        ["arc.pulse.occurrence.v1", check.name, slot], separators=(",", ":")
    ).encode()
    occurrence_id = hashlib.sha256(body).hexdigest()
    return "pulse_" + occurrence_id, occurrence_id


async def dispatch_signed_pulse(
    check: PulseCheck,
    *,
    prompt: str,
    due_at: datetime,
    tenant_id: str,
    agent_did: str,
    authority: ControlArtifactAuthority,
    issuer: RunTriggerIssuer,
    prepare: Callable[..., CanonicalRunRequest],
    run_fn: Callable[..., Awaitable[Any]],
) -> Any:
    """Freshly verify approval, issue exact run authorization, then invoke."""
    definition = canonical_definition(check)
    digest = hashlib.sha256(definition).hexdigest()
    approval = check.approval
    run_id, occurrence_id = pulse_occurrence(check, due_at)
    if approval is None or (
        approval.revoked
        or approval.tenant_id != tenant_id
        or approval.agent_did != agent_did
        or approval.purpose != "pulse"
        or approval.artifact_id != check.name
        or approval.definition_digest != digest
    ):
        raise ControlArtifactRefusedError("pulse approval does not bind its definition")
    try:
        await authority.verify_current(
            tenant_id=tenant_id,
            agent_did=agent_did,
            purpose="pulse",
            artifact_id=check.name,
            canonical_definition=definition,
            approval=approval,
            occurrence_id=occurrence_id,
        )
    except ControlArtifactRefusedError:
        raise
    except Exception as exc:
        raise ControlArtifactUnavailableError("pulse authority unavailable") from exc
    session_key = f"pulse:{check.name}"
    request = prepare(
        prompt,
        session_key=session_key,
        run_id=run_id,
        occurrence_id=occurrence_id,
        run_purpose="pulse",
        caller_did=approval.actor_did,
    )
    evidence = json.dumps(
        {
            "domain": "arc.pulse-trigger.v1",
            "approval": approval.model_dump(mode="json"),
            "due_at": due_at.isoformat(),
            "occurrence_id": occurrence_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    try:
        authorization, deadline = await issuer(request, evidence)
    except ControlArtifactRefusedError:
        raise
    except Exception as exc:
        raise ControlArtifactUnavailableError("pulse trigger issuer unavailable") from exc
    if deadline.tzinfo is None or deadline.utcoffset() is None or deadline <= datetime.now(UTC):
        raise ControlArtifactRefusedError("pulse run deadline invalid")
    result = await run_fn(
        prompt,
        session_key=session_key,
        run_id=run_id,
        occurrence_id=occurrence_id,
        run_purpose="pulse",
        caller_did=approval.actor_did,
        signed_authorization=authorization,
        authorization_deadline=deadline,
    )
    if getattr(result, "outcome_unknown", None) is not None:
        raise RunOutcomeUnknownError(run_id)
    return result
