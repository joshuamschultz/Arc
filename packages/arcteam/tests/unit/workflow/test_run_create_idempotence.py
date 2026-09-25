"""A repeated workflow occurrence must not erase durable progress."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend
from arcstore.mutation_fence import RunnerFence

from arcteam.workflow.stores import WorkflowRunStore

_RUN_ID = "schedule-slot-1"
_WORKFLOW_ID = "workflow-1"
_ACTOR = "did:arc:test:agent"


async def _create(
    store: WorkflowRunStore,
    *,
    content_hash: str = "sha256:definition-a",
    input: Mapping[str, Any] | None = None,  # noqa: A002
    trigger_digest: str | None = "schedule-revision-a",
) -> Any:
    return await store.create_run(
        run_id=_RUN_ID,
        workflow_id=_WORKFLOW_ID,
        version=1,
        content_hash=content_hash,
        trigger_digest=trigger_digest,
        initiator_did=_ACTOR,
        channel=None,
        input={"case": "one"} if input is None else input,
        budget_tokens=100,
        budget_cost_usd=None,
        budget_wall_clock_s=None,
    )


@pytest.mark.asyncio
async def test_redelivery_and_concurrent_create_preserve_existing_run() -> None:
    backend = FakeBackend()
    store = WorkflowRunStore(backend)
    first, second = await asyncio.gather(_create(store), _create(store))
    assert first.run_id == second.run_id
    assert await store.set_status(_RUN_ID, "done", actor_did=_ACTOR, expected_status="running")
    repeated = await _create(store)
    assert repeated.status == "done"
    record = await store.record(_RUN_ID)
    assert record is not None and record.status == "done"


@pytest.mark.asyncio
async def test_same_occurrence_changed_definition_or_input_is_refused() -> None:
    store = WorkflowRunStore(FakeBackend())
    await _create(store)
    with pytest.raises(ValueError, match="different definition"):
        await _create(store, content_hash="sha256:definition-b")
    with pytest.raises(ValueError, match="different definition"):
        await _create(store, input={"case": "two"})
    with pytest.raises(ValueError, match="different definition"):
        await _create(store, trigger_digest="schedule-revision-b")
    existing = await store.get(_RUN_ID)
    assert existing is not None and existing.input == {"case": "one"}


@pytest.mark.asyncio
async def test_lost_run_create_response_reconciles_companion_without_reset() -> None:
    class LostAckBackend(FakeBackend):
        lost = False

        async def mutable_create_batch(
            self,
            collection: str,
            entries: Sequence[tuple[str, dict[str, Any]]],
            *,
            actor_did: str,
            sink: Any | None = None,
            fence: RunnerFence | None = None,
        ) -> list[dict[str, Any]]:
            rows = await super().mutable_create_batch(
                collection, entries, actor_did=actor_did, sink=sink, fence=fence
            )
            if collection == "runs" and not self.lost:
                self.lost = True
                raise TimeoutError("committed but response lost")
            return rows

    backend = LostAckBackend()
    store = WorkflowRunStore(backend)
    with pytest.raises(TimeoutError):
        await _create(store)
    assert await store.record(_RUN_ID) is not None
    assert await store.get(_RUN_ID) is not None
    repeated = await _create(store)
    assert repeated.run_id == _RUN_ID
    assert len(await store.list_for_workflow(_WORKFLOW_ID)) == 1
