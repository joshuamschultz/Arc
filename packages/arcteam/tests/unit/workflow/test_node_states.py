"""P14-B step 1 — durable per-node state on the Run, written by revision CAS.

``Run.node_states`` is the snapshot the UI and resume read. Decisions still come
from the derived ``RunState``; the invariant pinned here is that after every
``advance`` the snapshot equals what the task rows plus the journal say.

The reference derivation below is written independently of the runner's, so
the invariant test can fail when the runner's own derivation drifts.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from arcstore.runs import NodeState
from arcstore.tasks import Task

from arcteam.workflow.runner import NodeStateConflict

from .conftest import (
    OPS_DID,
    SALES_DID,
    Definition,
    Node,
    Route,
    complete_node,
    fail_node,
    task_id,
)
from .test_runner_frontier import build

_ROW_STATUS = {
    "backlog": "materialized",
    "todo": "materialized",
    "in_progress": "in_progress",
    "review": "review",
    "done": "done",
    "failed": "failed",
}

BRANCHY = Definition(
    id="branchy",
    channel="channel://branchy",
    nodes=(
        Node(id="a", kind="agent", agent="@sales"),
        Node(
            id="r",
            kind="router",
            mode="rules",
            needs=("a",),
            routes=(
                Route(to="left", when="$nodes.a.output.go == 'left'"),
                Route(to="right", default=True),
            ),
        ),
        Node(id="left", kind="agent", agent="@ops", needs=("r",)),
        Node(id="right", kind="agent", agent="@ops", needs=("r",)),
        Node(id="c", kind="agent", agent="@sales", needs=("a",)),
        Node(
            id="e",
            kind="agent",
            agent="@ops",
            needs=("c",),
            when="$nodes.c.output.ok == true",
        ),
    ),
)

OWNER = {"a": SALES_DID, "c": SALES_DID, "left": OPS_DID, "right": OPS_DID, "e": OPS_DID}


def reference_node_states(
    rows: Sequence[Task], path: Sequence[Mapping[str, Any]]
) -> dict[str, NodeState]:
    """What the snapshot must say: latest iteration wins; route > row > skip on a tie."""
    best: dict[str, tuple[int, int, NodeState]] = {}

    def offer(node_id: str, iteration: int, rank: int, state: NodeState) -> None:
        current = best.get(node_id)
        if current is None or (iteration, rank) > (current[0], current[1]):
            best[node_id] = (iteration, rank, state)

    instances: dict[tuple[str, int], NodeState] = {}
    for row in rows:
        node_id = str(row.metadata["node_id"])
        iteration = int(row.metadata["iteration"])
        state = NodeState(
            status=_ROW_STATUS[row.status],  # type: ignore[arg-type]
            iteration=iteration,
            task_id=row.id,
            attempts=row.attempts,
            max_attempts=row.max_attempts,
            last_error=row.last_error,
            started_at=row.started_at or row.created_at,
            finished_at=row.completed_at,
        )
        instances[(node_id, iteration)] = state
        offer(node_id, iteration, 1, state)
    for entry in path:
        node_id = str(entry.get("node_id", ""))
        iteration = int(entry.get("iteration", 0))
        if entry.get("kind") == "skipped":
            offer(
                node_id,
                iteration,
                0,
                NodeState(status="skipped", iteration=iteration, reason=str(entry["reason"])),
            )
        elif entry.get("kind") == "cancelled":
            offer(
                node_id,
                iteration,
                0,
                NodeState(status="cancelled", iteration=iteration, reason=str(entry["reason"])),
            )
        elif entry.get("kind") == "route":
            base = instances.get((node_id, iteration)) or NodeState(
                status="routed", iteration=iteration
            )
            offer(
                node_id,
                iteration,
                2,
                base.model_copy(update={"status": "routed", "route": str(entry["chosen"])}),
            )
    return {node_id: state for node_id, (_, _, state) in best.items()}


async def _assert_snapshot_matches(stores: Any, run_id: str) -> None:
    flow_tasks, runs, _ = stores
    run = await runs.get(run_id)
    rows = await flow_tasks.query_by_flow_run(run_id)
    assert run.node_states == reference_node_states(rows, run.path_taken)


async def test_materialize_writes_node_state_with_task_id_and_started_at(
    stores: Any, registry: Any
) -> None:
    _, runs, _ = stores
    runner = build(stores, registry, BRANCHY)
    run = await runner.start_run(
        "branchy", input={}, initiator="operator", initiator_did="did:arc:x/1", run_id="run-m"
    )
    stored = await runs.get(run.run_id)
    state = stored.node_states["a"]
    assert state.status == "materialized"
    assert state.task_id == task_id("run-m", "a")
    assert state.started_at is not None
    assert stored.revision >= 1


async def test_skip_route_and_fail_write_their_states(stores: Any, registry: Any) -> None:
    _, runs, tasks = stores
    runner = build(stores, registry, BRANCHY)
    await runner.start_run(
        "branchy", input={}, initiator="operator", initiator_did="did:arc:x/1", run_id="run-s"
    )
    await complete_node(tasks, task_id("run-s", "a"), SALES_DID, {"go": "left"})
    await runner.advance("run-s")
    await complete_node(tasks, task_id("run-s", "c"), SALES_DID, {"ok": False})
    await runner.advance("run-s")
    await fail_node(tasks, task_id("run-s", "left"), OPS_DID, "provider exploded")
    await runner.advance("run-s")

    states = (await runs.get("run-s")).node_states
    assert states["r"].status == "routed" and states["r"].route == "left"
    assert states["right"].status == "skipped"
    assert states["right"].reason == "branch not taken"
    assert states["e"].status == "skipped" and states["e"].reason == "condition"
    assert states["left"].status == "failed"
    assert states["left"].last_error == "provider exploded"
    assert states["left"].finished_at is not None
    await _assert_snapshot_matches(stores, "run-s")


async def test_finalize_copies_attempts_and_last_error_from_settled_rows(
    stores: Any, registry: Any
) -> None:
    _, runs, tasks = stores
    runner = build(stores, registry, BRANCHY)
    await runner.start_run(
        "branchy", input={}, initiator="operator", initiator_did="did:arc:x/1", run_id="run-f"
    )
    row_id = task_id("run-f", "a")
    # One failed attempt requeued, then a second attempt that succeeds.
    await tasks.start_task(row_id, SALES_DID)
    await tasks.requeue(
        row_id, actor_did=SALES_DID, last_error="first try timed out", next_attempt_at="x"
    )
    await complete_node(tasks, row_id, SALES_DID, {"go": "right"})
    await runner.advance("run-f")

    state = (await runs.get("run-f")).node_states["a"]
    assert state.status == "done"
    assert state.attempts == 2
    assert state.last_error == "first try timed out"
    assert state.finished_at is not None


async def test_set_node_states_conflict_rereads_and_retries_then_raises(
    stores: Any, registry: Any
) -> None:
    _, runs, _ = stores
    runner = build(stores, registry, BRANCHY)
    real = runs.set_node_states
    calls: list[int] = []
    conflicts = {"left": 2}

    async def flaky(run_id: str, updates: Any, **kwargs: Any) -> Any:
        calls.append(kwargs["expected_revision"])
        if conflicts["left"] > 0:
            conflicts["left"] -= 1
            return None, "conflict"
        return await real(run_id, updates, **kwargs)

    runs.set_node_states = flaky  # type: ignore[method-assign]
    await runner.start_run(
        "branchy", input={}, initiator="operator", initiator_did="did:arc:x/1", run_id="run-c"
    )
    assert len(calls) >= 3, "two conflicts must be re-read and retried, not dropped"
    assert (await runs.get("run-c")).node_states["a"].status == "materialized"

    conflicts["left"] = 99
    await complete_node(stores[2], task_id("run-c", "a"), SALES_DID, {"go": "left"})
    with pytest.raises(NodeStateConflict):
        await runner.advance("run-c")


@pytest.mark.parametrize("seed", range(12))
async def test_node_states_equal_derived_state_after_every_tick(
    stores: Any, registry: Any, seed: int
) -> None:
    flow_tasks, runs, tasks = stores
    rng = random.Random(seed)  # noqa: S311 — a reproducible test schedule, not a secret
    runner = build(stores, registry, BRANCHY)
    run_id = f"run-p{seed}"
    await runner.start_run(
        "branchy", input={}, initiator="operator", initiator_did="did:arc:x/1", run_id=run_id
    )
    await _assert_snapshot_matches(stores, run_id)
    for _ in range(20):
        run = await runs.get(run_id)
        if run.status in ("done", "failed", "cancelled"):
            break
        open_rows = [
            row for row in await flow_tasks.query_by_flow_run(run_id) if row.status == "todo"
        ]
        rng.shuffle(open_rows)
        for row in open_rows[: rng.randint(1, max(1, len(open_rows)))]:
            node = str(row.metadata["node_id"])
            if rng.random() < 0.08:
                await fail_node(tasks, row.id, OWNER[node], "random failure")
                continue
            output: dict[str, Any] = {
                "go": rng.choice(["left", "right"]),
                "ok": rng.random() < 0.5,
            }
            await complete_node(tasks, row.id, OWNER[node], output)
        await runner.advance(run_id)
        await _assert_snapshot_matches(stores, run_id)
    assert (await runs.get(run_id)).status in ("done", "failed")


async def test_reconcile_repairs_divergent_snapshot(stores: Any, registry: Any) -> None:
    _, runs, tasks = stores
    runner = build(stores, registry, BRANCHY)
    await runner.start_run(
        "branchy", input={}, initiator="operator", initiator_did="did:arc:x/1", run_id="run-r"
    )
    await complete_node(tasks, task_id("run-r", "a"), SALES_DID, {"go": "left"})
    await runner.advance("run-r")
    run = await runs.get("run-r")
    # Hand-edit the snapshot to a stale value, as a crash between a row write and
    # the snapshot write would leave it.
    await runs.set_node_states(
        "run-r",
        {"a": NodeState(status="in_progress", task_id=task_id("run-r", "a"))},
        actor_did="did:arc:x/1",
        expected_revision=run.revision,
    )
    await runner.advance("run-r")
    assert (await runs.get("run-r")).node_states["a"].status == "done"
    await _assert_snapshot_matches(stores, "run-r")
