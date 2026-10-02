"""Broker-verified schedule occurrence dispatch into an accepted ArcAgent run."""

from __future__ import annotations

import asyncio
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
from arcagent.modules.scheduler.models import CHANNEL_TARGET_PREFIX, ScheduleEntry
from arcagent.modules.scheduler.occurrence import scheduled_occurrence

AgentRunFn = Callable[..., Awaitable[Any]]
TeamSend = Callable[[str, str], Awaitable[None]]
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
    start_timeout: float | None = None,
    team_send: TeamSend | None = None,
) -> Any:
    """Verify the current signed revision before admitting one immutable due slot.

    ``start_timeout`` bounds only the start: verification, trigger issuance and a
    workflow run's creation. The model run itself is never covered by it; that is
    bounded by the signed run deadline the issuer returns.
    """
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
        await asyncio.wait_for(
            authority.verify_current(
                tenant_id=tenant_id,
                agent_did=agent_did,
                purpose="schedule",
                artifact_id=entry.id,
                canonical_definition=occurrence.definition,
                approval=approval,
                occurrence_id=occurrence.occurrence_id,
            ),
            start_timeout,
        )
    except ControlArtifactRefusedError:
        raise
    except Exception as exc:
        raise ControlArtifactUnavailableError("schedule authority unavailable") from exc
    if entry.action == "workflow_run":
        from arcagent.modules.workflows.run_entry import start_workflow_run

        if entry.workflow_id is None:
            raise ControlArtifactRefusedError("workflow schedule has no workflow identity")
        return await asyncio.wait_for(
            start_workflow_run(
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
            ),
            start_timeout,
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
        authorization, deadline = await asyncio.wait_for(issuer(request, evidence), start_timeout)
    except ControlArtifactRefusedError:
        raise
    except Exception as exc:
        raise ControlArtifactUnavailableError("scheduled trigger issuer unavailable") from exc
    if deadline.tzinfo is None or deadline.utcoffset() is None or deadline <= datetime.now(UTC):
        raise ControlArtifactRefusedError("scheduled run deadline invalid")
    result = await run_fn(
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
    if getattr(result, "outcome_unknown", None) is not None:
        raise RunOutcomeUnknownError(occurrence.run_id)
    await _deliver_to_channel(entry, result, team_send)
    return result


async def _deliver_to_channel(
    entry: ScheduleEntry, result: Any, team_send: TeamSend | None
) -> None:
    """Post the run's final text to a canonical ``channel://`` target on the team bus.

    Gateway ``platform:chat_id`` targets keep their own delivery path; only the
    team-bus scheme lands here. A channel target with no sender wired fails
    closed rather than finishing a run whose output silently goes nowhere.
    """
    target = entry.deliver_to
    if target is None or not target.startswith(CHANNEL_TARGET_PREFIX):
        return
    if team_send is None:
        raise ControlArtifactUnavailableError("team delivery unavailable for channel target")
    text = str(getattr(result, "content", None) or result or "").strip()
    if text:
        await team_send(target, text)
