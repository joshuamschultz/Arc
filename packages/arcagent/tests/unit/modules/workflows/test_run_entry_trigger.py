"""T-1345 / T-1346 — a scheduled workflow run names its trigger (SPEC-088 COMP-007).

``start_workflow_run`` passes ``TriggerContext("cron" | "interval")`` to the
control plane from the schedule entry type, so the runner validates the input
and records ``arc_trigger`` truthfully (D-843). The scheduler's signed dispatch
hands the entry type down as ``trigger_kind``.

Seam choice (flagged for the orchestrator): ``start_workflow_run`` gains a
keyword ``trigger_kind`` and builds arcteam's ``TriggerContext`` itself, lazily,
the same way ``_runtime`` imports arcteam — arcagent never imports arcteam at
module scope.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from arcteam.workflow.runner_contracts import TriggerContext
from packages.arcagent.tests.unit.modules.workflows.conftest import (
    FakeResult,
    RecordingControlPlane,
)

from arcagent.core.control_contract import SignedControlRevision
from arcagent.modules.scheduler.models import ScheduleEntry
from arcagent.modules.scheduler.occurrence import canonical_definition
from arcagent.modules.scheduler.signed_dispatch import dispatch_signed_schedule


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["cron", "interval"])
async def test_start_workflow_run_passes_the_schedule_trigger_context(
    workflows_state: RecordingControlPlane, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from arcagent.modules.workflows.run_entry import start_workflow_run

    seen: list[dict[str, Any]] = []

    async def spy_run(workflow_id: str, **kwargs: Any) -> FakeResult:
        seen.append({"workflow_id": workflow_id, **kwargs})
        return FakeResult(ok=True, run={"run_id": "schedule_x", "status": "running"})

    monkeypatch.setattr(workflows_state, "run", spy_run, raising=False)

    await start_workflow_run(
        "nightly",
        {"case": "one"},
        run_id="schedule_x",
        trigger_digest="digest",
        trigger_kind=kind,
    )

    assert len(seen) == 1
    assert seen[0]["initiator"] == "scheduler"
    assert seen[0]["trigger"] == TriggerContext(kind)  # type: ignore[arg-type]  # parametrized
    assert seen[0]["trigger"].name is None


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
@pytest.mark.parametrize(
    ("kind", "timing"),
    [("cron", {"expression": "0 12 * * *"}), ("interval", {"every_seconds": 60})],
)
async def test_signed_dispatch_hands_the_entry_type_down_as_trigger_kind(
    monkeypatch: pytest.MonkeyPatch, kind: str, timing: dict[str, Any]
) -> None:
    calls: list[dict[str, Any]] = []

    async def spy_start(workflow_id: str, workflow_input: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append({"workflow_id": workflow_id, **kwargs})
        return {"run_id": kwargs.get("run_id", ""), "workflow_id": workflow_id}

    monkeypatch.setattr("arcagent.modules.workflows.run_entry.start_workflow_run", spy_start)
    entry = _approved(
        ScheduleEntry(
            id=f"wf-{kind}",
            type=kind,  # type: ignore[arg-type]  # parametrized Literal
            action="workflow_run",
            workflow_id="workflow-1",
            workflow_input={"case": "one"},
            **timing,
        )
    )

    await dispatch_signed_schedule(
        entry,
        now=datetime(2026, 9, 25, 12, 0, 2, tzinfo=UTC),
        default_timezone="UTC",
        tenant_id="tenant",
        agent_did="did:arc:local:agent/one",
        authority=AsyncMock(),
        issuer=None,
        prepare=None,
        run_fn=None,
    )

    assert len(calls) == 1
    assert calls[0]["trigger_kind"] == kind
