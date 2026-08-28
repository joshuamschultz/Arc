"""The runs a reader renders come back newest-first.

The store returns run rows in an opaque insertion order — a list a person
cannot follow. Every surface reads a workflow's runs through
``WorkflowRunStore.list_for_workflow``, so that one seam sorts them newest-first
by start time (``created_at``, an ISO-8601 string).
"""

from __future__ import annotations

import pytest
from arcstore.backends.memory import FakeBackend
from arcstore.runs import Run

from arcteam.workflow.stores import WorkflowRunStore


def _run(run_id: str, created_at: str) -> Run:
    return Run(
        id=run_id,
        workflow_id="wf",
        workflow_version=1,
        content_hash="h",
        initiator_did="did:arc:test",
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_list_for_workflow_returns_newest_first() -> None:
    backend = FakeBackend()
    await backend.start()
    store = WorkflowRunStore(backend)
    # Seeded in a deliberately jumbled order, with distinct timestamps.
    for run in (
        _run("mid", "2026-08-20T06:30:00+00:00"),
        _run("newest", "2026-08-27T19:40:00+00:00"),
        _run("oldest", "2026-08-17T09:51:00+00:00"),
    ):
        await backend.mutable_write("runs", run.id, run.model_dump(mode="json"), actor_did="op")

    runs = await store.list_for_workflow("wf")

    assert [run.id for run in runs] == ["newest", "mid", "oldest"]
