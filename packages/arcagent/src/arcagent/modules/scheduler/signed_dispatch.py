"""Broker-verified schedule occurrence dispatch into an accepted ArcAgent run."""

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
from arcagent.core.run_contract import CanonicalRunRequest, RunTriggerIssuer
from arcagent.modules.scheduler.models import ScheduleEntry
from arcagent.modules.scheduler.occurrence import scheduled_occurrence

AgentRunFn = Callable[..., Awaitable[Any]]
PrepareRun = Callable[..., CanonicalRunRequest]


async def dispatch_signed_schedule(
    entry: ScheduleEntry,
    *,
    now: datetime,
    default_timezone: str,
    tenant_id: str,
    agent_did: str,
    authority: ControlArtifactAuthority,
    issuer: RunTriggerIssuer | None,
    prepare: PrepareRun | None,
    run_fn: AgentRunFn | None,
) -> Any:
    """Verify the current signed revision before admitting one immutable due slot."""
    occurrence = scheduled_occurrence(entry, now, default_timezone=default_timezone)
    approval = entry.approval
    if approval is None or (
        approval.revoked
        or approval.tenant_id != tenant_id
        or approval.agent_did != agent_did
        or approval.purpose != "schedule"
        or approval.artifact_id != entry.id
        or approval.definition_digest != hashlib.sha256(occurrence.definition).hexdigest()
    ):
        raise ControlArtifactRefusedError("schedule approval does not bind its definition")
    try:
        await authority.verify_current(
            tenant_id=tenant_id,
            agent_did=agent_did,
            purpose="schedule",
            artifact_id=entry.id,
            canonical_definition=occurrence.definition,
            approval=approval,
            occurrence_id=occurrence.occurrence_id,
        )
    except ControlArtifactRefusedError:
        raise
    except Exception as exc:
        raise ControlArtifactUnavailableError("schedule authority unavailable") from exc
    if entry.action == "workflow_run":
        from arcagent.modules.workflows.run_entry import start_workflow_run

        if entry.workflow_id is None:
            raise ControlArtifactRefusedError("workflow schedule has no workflow identity")
        return await start_workflow_run(
            entry.workflow_id,
            entry.workflow_input,
            run_id=occurrence.run_id,
            trigger_digest=hashlib.sha256(
                json.dumps(
                    {
                        "domain": "arc.scheduled-workflow.v1",
                        "occurrence": json.loads(occurrence.evidence),
                        "approval": approval.model_dump(mode="json"),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        )
    if issuer is None or prepare is None or run_fn is None:
        raise ControlArtifactUnavailableError("signed prompt run capability unavailable")
    session_key = f"scheduler:{entry.id}"
    reply_label = f"Schedule — {entry.label}"
    request = prepare(
        entry.prompt,
        session_key=session_key,
        run_id=occurrence.run_id,
        occurrence_id=occurrence.occurrence_id,
        run_purpose="schedule",
        caller_did=approval.actor_did,
        reply_target=entry.deliver_to,
        reply_label=reply_label,
    )
    evidence = json.dumps(
        {
            "domain": "arc.scheduled-trigger.v1",
            "occurrence": json.loads(occurrence.evidence),
            "approval": approval.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    try:
        authorization, deadline = await issuer(request, evidence)
    except ControlArtifactRefusedError:
        raise
    except Exception as exc:
        raise ControlArtifactUnavailableError("scheduled trigger issuer unavailable") from exc
    if deadline.tzinfo is None or deadline.utcoffset() is None or deadline <= datetime.now(UTC):
        raise ControlArtifactRefusedError("scheduled run deadline invalid")
    return await run_fn(
        entry.prompt,
        session_key=session_key,
        run_id=occurrence.run_id,
        occurrence_id=occurrence.occurrence_id,
        run_purpose="schedule",
        caller_did=approval.actor_did,
        reply_target=entry.deliver_to,
        reply_label=reply_label,
        signed_authorization=authorization,
        authorization_deadline=deadline,
    )
