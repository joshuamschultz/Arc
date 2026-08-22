"""PostgreSQL concurrency regressions for the ArcFlow durable plane."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from arcteam.workflow.stores import WorkflowRunStore, WorkflowTaskStore

from arcstore.backends.base import ArcStoreBackend
from arcstore.tasks import Task

_ACTOR = "did:arc:test:workflow-runner"


async def _run_store(backend: ArcStoreBackend) -> tuple[WorkflowRunStore, str]:
    runs = WorkflowRunStore(backend)
    run_id = f"workflow-race-{uuid4().hex}"
    await runs.create_run(
        run_id=run_id,
        workflow_id="workflow-race",
        version=1,
        content_hash="sha256:test",
        initiator_did=_ACTOR,
        channel=None,
        input={},
        budget_tokens=100,
        budget_cost_usd=None,
        budget_wall_clock_s=None,
    )
    return runs, run_id


@pytest.mark.asyncio
async def test_concurrent_workflow_journal_appends_are_lossless(
    postgres_backend: ArcStoreBackend,
) -> None:
    """Two runner ticks must not replace each other's journal entries."""
    runs, run_id = await _run_store(postgres_backend)
    entries = [
        {"kind": "skipped", "node_id": "a", "iteration": 0, "reason": "condition"},
        {"kind": "skipped", "node_id": "b", "iteration": 0, "reason": "condition"},
    ]

    await asyncio.gather(*(runs.append_path(run_id, entry, actor_did=_ACTOR) for entry in entries))

    state = await postgres_backend.mutable_read("workflow_run_state", run_id)
    record = await runs.record(run_id)
    assert state is not None and record is not None
    assert {entry["node_id"] for entry in state["path"]} == {"a", "b"}
    assert {entry.node_id for entry in record.path_taken} == {"a", "b"}


@pytest.mark.asyncio
async def test_concurrent_duplicate_journal_appends_are_idempotent(
    postgres_backend: ArcStoreBackend,
) -> None:
    """Retries of one tick must not duplicate its materialization event."""
    for _ in range(20):
        runs, run_id = await _run_store(postgres_backend)
        entry = {"kind": "skipped", "node_id": "a", "iteration": 0, "reason": "condition"}

        await asyncio.gather(
            *(runs.append_path(run_id, entry, actor_did=_ACTOR) for _ in range(8))
        )

        state = await postgres_backend.mutable_read("workflow_run_state", run_id)
        record = await runs.record(run_id)
        assert state is not None and record is not None
        assert state["path"] == [entry]
        assert [path.node_id for path in record.path_taken] == ["a"]


@pytest.mark.asyncio
async def test_concurrent_workflow_settlement_is_idempotent(
    postgres_backend: ArcStoreBackend,
) -> None:
    """Concurrent runner retries settle one terminal node exactly once."""
    runs, run_id = await _run_store(postgres_backend)

    await asyncio.gather(
        *(
            runs.record_spend(
                run_id,
                tokens=30,
                cost_usd=0.0,
                settlement_key="a:0",
                actor_did=_ACTOR,
            )
            for _ in range(8)
        )
    )

    record = await runs.record(run_id)
    assert record is not None
    assert record.budget.tokens_spent == 30
    assert record.settled == ["a:0"]


@pytest.mark.asyncio
async def test_concurrent_gate_resolution_has_one_winner(
    postgres_backend: ArcStoreBackend,
) -> None:
    """A gate's reviewer decision is a status-conditional CAS, not a merge."""
    tasks = WorkflowTaskStore(postgres_backend, actor_did=_ACTOR)
    task_id = f"gate-{uuid4().hex}"
    await tasks.create_batch(
        [
            Task(
                id=task_id,
                title="workflow gate",
                status="review",
                creator_did=_ACTOR,
                requires_review=True,
                metadata={"node_kind": "gate", "flow_run_id": "run"},
            )
        ],
        actor_did=_ACTOR,
    )

    async def resolve(decision: str) -> bool:
        task = await tasks.get(task_id)
        assert task is not None
        return (
            await tasks.update_if(
                task_id,
                {
                    "status": "done",
                    "metadata": {**task.metadata, "gate_decision": decision},
                },
                where={"status": "review"},
                actor_did=_ACTOR,
            )
            is not None
        )

    first, second = await asyncio.gather(resolve("approved"), resolve("rejected"))
    assert (first, second) in {(True, False), (False, True)}
    final = await tasks.get(task_id)
    assert final is not None
    assert final.metadata["gate_decision"] in {"approved", "rejected"}
