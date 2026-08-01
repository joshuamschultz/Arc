"""The test doubles are a CLAIM about the real stores. Verify the claim.

Most of the runner's unit tests run against ``conftest``'s ``FlowRunStore`` and
``FlowTaskStore``, which were written before arcstore's ``RunStore`` and
``create_batch`` existed. A double written before the real thing drifts toward
whatever made the test pass rather than toward what the real thing does, and
every technique used elsewhere in this suite is blind to that: the doubles have
no swallows to audit, mutating them correctly reddens tests, and the assertions
are about outcomes rather than wiring. Only running both against the same
operations catches it.

So this file pins the contract points the runner actually depends on. If the
real adapter's behaviour changes and the double's does not, this goes red
instead of the runner's unit tests quietly asserting something untrue.
"""

from __future__ import annotations

from typing import Any

import pytest
from arcstore.backends.sqlite import SqliteBackend
from arcstore.tasks import Task, TaskStore

from arcteam.workflow.stores import WorkflowRunStore, WorkflowTaskStore

from ..unit.workflow.conftest import FlowRunStore, FlowTaskStore

RUNNER = "did:arc:local:workflow-runner/abcd1234"
OWNER = "did:arc:local:agent/1111aaaa"


def _row(run_id: str, node_id: str) -> Task:
    return Task(
        id=f"wf/{run_id}/{node_id}/0",
        title=f"conformance: {node_id}",
        owner_did=OWNER,
        creator_did=RUNNER,
        status="todo",
        metadata={"flow_run_id": run_id, "node_id": node_id, "iteration": 0},
    )


@pytest.fixture
async def pair(tmp_path: Any) -> Any:
    """The real adapter and the double, each over its own real backend."""
    real_backend = SqliteBackend(tmp_path / "real.db")
    await real_backend.start()
    double_backend = SqliteBackend(tmp_path / "double.db")
    await double_backend.start()

    real = (
        WorkflowTaskStore(real_backend, actor_did=RUNNER),
        WorkflowRunStore(real_backend),
    )
    double = (
        FlowTaskStore(double_backend, TaskStore(double_backend)),
        FlowRunStore(double_backend),
    )
    yield real, double
    await real_backend.stop()
    await double_backend.stop()


async def _create(runs: Any, run_id: str, **kwargs: Any) -> Any:
    return await runs.create_run(
        run_id=run_id,
        workflow_id="conformance",
        version=1,
        content_hash="sha256:x",
        initiator_did="did:arc:local:user/9",
        channel=None,
        input={},
        budget_tokens=kwargs.get("budget_tokens"),
        budget_cost_usd=None,
        budget_wall_clock_s=None,
    )


async def test_create_batch_is_idempotent_on_the_row_id_in_both(pair: Any) -> None:
    """The runner's whole no-double-materialization guarantee rests on this."""
    for tasks, _ in pair:
        first = await tasks.create_batch([_row("r1", "a")], actor_did=RUNNER)
        again = await tasks.create_batch([_row("r1", "a")], actor_did=RUNNER)

        assert len(first) == 1
        assert len(again) == 1
        assert again[0].id == first[0].id
        assert again[0].created_at == first[0].created_at, "a re-create must not restamp"


async def test_query_by_flow_run_is_scoped_in_both(pair: Any) -> None:
    """A tick must see its own run's rows and nobody else's."""
    for tasks, _ in pair:
        await tasks.create_batch(
            [_row("r1", "a"), _row("r2", "b")], actor_did=RUNNER
        )

        scoped = await tasks.query_by_flow_run("r1")

        assert [t.metadata["node_id"] for t in scoped] == ["a"]


async def test_set_status_is_conditional_in_both(pair: Any) -> None:
    """The cancel/terminate race depends on a mismatched expectation LOSING."""
    for _, runs in pair:
        await _create(runs, "r1")

        lost = await runs.set_status(
            "r1", "done", actor_did=RUNNER, expected_status="waiting_gate"
        )
        won = await runs.set_status(
            "r1", "done", actor_did=RUNNER, expected_status="running"
        )

        assert lost is False, "a stale expectation must not win"
        assert won is True
        record = await runs.get("r1")
        assert record is not None and record.status == "done"


async def test_spend_accumulates_in_both(pair: Any) -> None:
    """Budget enforcement reads this number; the two must agree on it."""
    for _, runs in pair:
        await _create(runs, "r1", budget_tokens=100)

        await runs.record_spend("r1", tokens=30, cost_usd=0.0, actor_did=RUNNER)
        await runs.record_spend("r1", tokens=12, cost_usd=0.0, actor_did=RUNNER)

        record = await runs.get("r1")
        assert record is not None and record.tokens_spent == 42


async def test_the_path_journal_round_trips_in_both(pair: Any) -> None:
    """Re-derivation reads these entries back; a lossy double would hide a bug."""
    for _, runs in pair:
        await _create(runs, "r1")

        await runs.append_path(
            "r1", {"kind": "materialized", "node_id": "a", "iteration": 0}, actor_did=RUNNER
        )
        await runs.append_path(
            "r1", {"kind": "skipped", "node_id": "b", "iteration": 0}, actor_did=RUNNER
        )

        record = await runs.get("r1")
        assert record is not None
        assert [e["kind"] for e in record.path_taken] == ["materialized", "skipped"]
        assert [e["node_id"] for e in record.path_taken] == ["a", "b"]


async def test_the_one_documented_divergence_is_the_missing_state_row(
    pair: Any,
) -> None:
    """The double is deliberately laxer here, and the real behaviour is the tested one.

    Recording it rather than silently tolerating it: the real adapter RAISES on
    a missing journal row because dropping an entry mis-accounts spend. The
    double predates that and returns. No unit test reaches this branch — the
    integration suite covers the real one — but an undocumented divergence is
    how a double starts drifting.
    """
    from arcteam.workflow.stores import RunStateMissingError

    (_, real_runs), (_, double_runs) = pair
    await _create(real_runs, "r1")
    await _create(double_runs, "r1")

    with pytest.raises(RunStateMissingError):
        await real_runs.append_path("gone", {"kind": "settled"}, actor_did=RUNNER)

    await double_runs.append_path("gone", {"kind": "settled"}, actor_did=RUNNER)


async def test_the_run_reference_count_agrees_in_both(pair: Any) -> None:
    """The purge guard reads this number; destroying history depends on it."""
    for _, runs in pair:
        await _create(runs, "r1")
        await _create(runs, "r2")

        assert await runs.count_runs_for_workflow("conformance") == 2
        assert await runs.count_runs_for_workflow("other") == 0
