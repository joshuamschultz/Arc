"""P14-B step 4 (item 65): the scheduler, the signed trigger and the runner agree on ``run_id``.

A schedule fire derives one id from its occurrence. That id has to be the same
three places: the run the workflow runner creates, the request the operator's
``LocalRunTriggerIssuer`` signs for a prompt run of the same occurrence, and the
row a repeated fire lands on. If any of them diverged, a double fire would start
two runs, or a signed trigger would authorize a run the runner never made.

Real: ``scheduled_occurrence``, ``CanonicalRunRequest``, ``LocalRunTriggerIssuer``
and ``build_workflow_runner`` over a real task/run store. Nothing is mocked.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from arcagent.core.run_contract import CanonicalRunRequest
from arcagent.modules.scheduler.models import ScheduleEntry
from arcagent.modules.scheduler.occurrence import scheduled_occurrence
from arcstore.backends.memory import FakeBackend
from arcteam.workflow.runner import build_workflow_runner
from arctrust import LocalRunTriggerIssuer, OperatorKey

_OWNER_DID = "did:arc:test:sales"
_NIGHTLY = """
[workflow]
id = "nightly"
version = 1
owner = "@sales"

[[node]]
id = "collect"
kind = "agent"
agent = "@sales"
"""


class _Entity:
    did = _OWNER_DID


class _Registry:
    async def get(self, handle: str) -> _Entity | None:
        return _Entity() if handle == "sales" else None


@pytest.fixture
async def runner(tmp_path: Path) -> AsyncIterator[Any]:
    OperatorKey.generate().save(tmp_path / "operator" / "operator.key")
    bundle = tmp_path / "workflows" / "nightly"
    bundle.mkdir(parents=True)
    (bundle / "workflow.toml").write_text(_NIGHTLY, encoding="utf-8")
    backend = FakeBackend()
    await backend.start()
    built = build_workflow_runner(
        tier="personal",
        task_store_backend=backend,
        runner_key_path=tmp_path / "operator" / "operator.key",
        workspace_root=tmp_path,
        registry=_Registry(),
    )
    yield built
    await built.aclose()
    await backend.stop()


def _request(run_id: str, occurrence_id: str) -> CanonicalRunRequest:
    return CanonicalRunRequest(
        run_id=run_id,
        session_key="scheduler:wf:nightly",
        input_text="run nightly",
        purpose="schedule",
        occurrence_id=occurrence_id,
    )


async def test_occurrence_run_id_is_the_runner_run_id_and_the_signed_request_run_id(
    runner: Any,
) -> None:
    entry = ScheduleEntry(
        id="wf:nightly",
        type="cron",
        action="workflow_run",
        workflow_id="nightly",
        expression="0 22 * * *",
        timezone="America/Chicago",
    )
    fired_at = datetime(2026, 10, 2, 3, 30, tzinfo=UTC)  # 22:30 Central, after the 22:00 slot
    occurrence = scheduled_occurrence(entry, fired_at)
    # A restarted scheduler recomputes the same slot a minute later.
    restarted = scheduled_occurrence(entry, datetime(2026, 10, 2, 3, 31, tzinfo=UTC))
    assert restarted.run_id == occurrence.run_id

    # The runner creates the run under exactly that id, and a repeated fire adds no second run.
    first = await runner.start_run(
        "nightly",
        input={},
        initiator="operator",
        initiator_did=_OWNER_DID,
        run_id=occurrence.run_id,
        trigger_digest=occurrence.definition_digest,
        detached=True,
    )
    second = await runner.start_run(
        "nightly",
        input={},
        initiator="operator",
        initiator_did=_OWNER_DID,
        run_id=restarted.run_id,
        trigger_digest=restarted.definition_digest,
        detached=True,
    )
    assert first.run_id == second.run_id == occurrence.run_id

    # The operator's signed trigger for the same occurrence binds a request carrying that id.
    issuer = LocalRunTriggerIssuer(OperatorKey.generate().into_signer())
    request = _request(occurrence.run_id, occurrence.occurrence_id)
    authorization, _deadline = await issuer(request, occurrence.evidence)
    signed = json.loads(authorization)
    assert signed["request_digest"] == request.digest()
    assert json.loads(request.canonical_bytes())["run_id"] == first.run_id
    # ...and a request for any other run id would not carry that signature.
    assert (
        _request("schedule_other", occurrence.occurrence_id).digest() != signed["request_digest"]
    )
