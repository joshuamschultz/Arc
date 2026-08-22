"""A stale ArcFlow owner cannot mutate durable workflow state after replacement."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from arcteam.workflow.stores import WorkflowRunStore, WorkflowTaskStore

from arcstore.backends.memory import FakeBackend
from arcstore.mutation_fence import MutationFenceRejectedError
from arcstore.tasks import Task
from arcstore.workflow_lease import WorkflowRunnerLease

_ACTOR = "did:arc:test:workflow-runner"


@pytest.mark.asyncio
async def test_stale_runner_fence_rejects_every_runner_owned_mutation() -> None:
    """The check and each mutation share FakeBackend's one lock, never a preflight."""
    backend = FakeBackend()
    now = datetime.now(UTC)
    old = WorkflowRunnerLease(backend, owner_id="old", clock=lambda: now)
    old_fence = await old.acquire_or_renew()
    assert old_fence is not None

    runs = WorkflowRunStore(backend)
    tasks = WorkflowTaskStore(backend, actor_did=_ACTOR)
    await runs.create_run(
        run_id="fenced-run",
        workflow_id="wf",
        version=1,
        content_hash="sha256:test",
        initiator_did=_ACTOR,
        channel=None,
        input={},
        budget_tokens=10,
        budget_cost_usd=None,
        budget_wall_clock_s=None,
        fence=old_fence,
    )
    await tasks.create_batch(
        [Task(id="existing", title="existing", creator_did=_ACTOR)],
        actor_did=_ACTOR,
        fence=old_fence,
    )

    replacement = WorkflowRunnerLease(
        backend, owner_id="replacement", clock=lambda: now + timedelta(seconds=61)
    )
    new_fence = await replacement.acquire_or_renew()
    assert new_fence is not None and new_fence.token > old_fence.token

    with pytest.raises(MutationFenceRejectedError):
        await tasks.create_batch(
            [Task(id="stale-create", title="stale", creator_did=_ACTOR)],
            actor_did=_ACTOR,
            fence=old_fence,
        )
    with pytest.raises(MutationFenceRejectedError):
        await tasks.update("existing", {"title": "stale update"}, actor_did=_ACTOR, fence=old_fence)
    with pytest.raises(MutationFenceRejectedError):
        await runs.set_status(
            "fenced-run", "done", actor_did=_ACTOR, expected_status="running", fence=old_fence
        )
    with pytest.raises(MutationFenceRejectedError):
        await runs.append_path("fenced-run", {"kind": "skipped", "node_id": "a"}, actor_did=_ACTOR, fence=old_fence)
    with pytest.raises(MutationFenceRejectedError):
        await runs.record_spend(
            "fenced-run", tokens=3, cost_usd=0.0, settlement_key="a:0", actor_did=_ACTOR, fence=old_fence
        )

    assert await tasks.get("stale-create") is None
    assert (await tasks.get("existing")).title == "existing"  # type: ignore[union-attr]
    record = await runs.record("fenced-run")
    assert record is not None
    assert record.status == "running"
    assert record.path_taken == []
    assert record.budget.tokens_spent == 0

    # Operator cancellation is deliberately not a runner-owned mutation.
    assert await runs.set_status(
        "fenced-run", "cancelled", actor_did="did:arc:operator", expected_status="running"
    )
