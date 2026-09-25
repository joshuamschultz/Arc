"""Schedule effects require current signed definition and exact due-slot proof."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from arcstore.backends.memory import FakeBackend
from arcteam.workflow.stores import WorkflowRunStore

from arcagent.core.control_contract import (
    ControlArtifactRefusedError,
    SignedControlRevision,
)
from arcagent.core.run_contract import CanonicalRunRequest
from arcagent.modules.scheduler.models import ScheduleEntry
from arcagent.modules.scheduler.occurrence import canonical_definition
from arcagent.modules.scheduler.signed_dispatch import dispatch_signed_schedule


def _approved(entry: ScheduleEntry) -> ScheduleEntry:
    return entry.model_copy(
        update={
            "approval": SignedControlRevision(
                tenant_id="tenant",
                agent_did="did:arc:local:agent/one",
                purpose="schedule",
                artifact_id=entry.id,
                revision=1,
                definition_digest=hashlib.sha256(canonical_definition(entry)).hexdigest(),
                actor_did="did:arc:local:user/operator",
                issued_at=datetime.now(UTC),
                signature="ab" * 64,
            )
        }
    )


@pytest.mark.asyncio
async def test_approved_occurrence_checks_current_head_before_exact_run() -> None:
    entry = _approved(
        ScheduleEntry(id="s1", type="interval", prompt="Check status", every_seconds=60)
    )
    authority = AsyncMock()
    requests: list[CanonicalRunRequest] = []

    def prepare(prompt: str, **kwargs: Any) -> CanonicalRunRequest:
        kwargs["purpose"] = kwargs.pop("run_purpose")
        request = CanonicalRunRequest(input_text=prompt, **kwargs)
        requests.append(request)
        return request

    async def issuer(request: CanonicalRunRequest, evidence: bytes) -> tuple[bytes, datetime]:
        parsed = json.loads(evidence)
        assert parsed["domain"] == "arc.scheduled-trigger.v1"
        assert parsed["approval"]["revision"] == 1
        assert parsed["occurrence"]["occurrence_id"] == request.occurrence_id
        return request.digest().encode(), datetime.now(UTC) + timedelta(minutes=1)

    run_fn = AsyncMock(return_value="finished")
    result = await dispatch_signed_schedule(
        entry,
        now=datetime(2026, 9, 25, 12, 0, 2, tzinfo=UTC),
        default_timezone="UTC",
        tenant_id="tenant",
        agent_did="did:arc:local:agent/one",
        authority=authority,
        issuer=issuer,
        prepare=prepare,
        run_fn=run_fn,
    )
    assert result == "finished"
    authority.verify_current.assert_awaited_once()
    assert run_fn.await_args is not None
    assert run_fn.await_args.kwargs["run_id"] == requests[0].run_id
    assert run_fn.await_args.kwargs["signed_authorization"] == requests[0].digest().encode()


@pytest.mark.asyncio
async def test_changed_definition_is_refused_before_authority_or_effect() -> None:
    original = _approved(
        ScheduleEntry(id="s1", type="interval", prompt="Check status", every_seconds=60)
    )
    changed = original.model_copy(update={"prompt": "Send secrets"})
    authority = AsyncMock()
    run_fn = AsyncMock()
    with pytest.raises(ControlArtifactRefusedError):
        await dispatch_signed_schedule(
            changed,
            now=datetime(2026, 9, 25, 12, 0, 2, tzinfo=UTC),
            default_timezone="UTC",
            tenant_id="tenant",
            agent_did="did:arc:local:agent/one",
            authority=authority,
            issuer=AsyncMock(),
            prepare=AsyncMock(),
            run_fn=run_fn,
        )
    authority.verify_current.assert_not_awaited()
    run_fn.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_signed_revision_same_due_slot_cannot_reuse_workflow_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _approved(
        ScheduleEntry(
            id="wf-slot",
            type="interval",
            action="workflow_run",
            workflow_id="workflow-1",
            workflow_input={"case": "one"},
            every_seconds=60,
        )
    )
    assert original.approval is not None
    edited = original.model_copy(
        update={
            "approval": original.approval.model_copy(
                update={"revision": 2, "signature": "cd" * 64}
            )
        }
    )
    store = WorkflowRunStore(FakeBackend())

    async def start_workflow_run(
        workflow_id: str,
        workflow_input: dict[str, Any] | None,
        *,
        run_id: str,
        trigger_digest: str,
    ) -> Any:
        return await store.create_run(
            run_id=run_id,
            workflow_id=workflow_id,
            version=1,
            content_hash="sha256:same-workflow-definition",
            trigger_digest=trigger_digest,
            initiator_did="did:arc:local:agent/one",
            channel=None,
            input=workflow_input or {},
            budget_tokens=None,
            budget_cost_usd=None,
            budget_wall_clock_s=None,
        )

    monkeypatch.setattr(
        "arcagent.modules.workflows.run_entry.start_workflow_run", start_workflow_run
    )
    authority = AsyncMock()

    async def dispatch(entry: ScheduleEntry) -> Any:
        return await dispatch_signed_schedule(
            entry,
            now=datetime(2026, 9, 25, 12, 0, 2, tzinfo=UTC),
            default_timezone="UTC",
            tenant_id="tenant",
            agent_did="did:arc:local:agent/one",
            authority=authority,
            issuer=None,
            prepare=None,
            run_fn=None,
        )

    first = await dispatch(original)
    with pytest.raises(ValueError, match="different definition"):
        await dispatch(edited)
    existing = await store.get(first.run_id)
    assert existing is not None and existing.content_hash == "sha256:same-workflow-definition"
