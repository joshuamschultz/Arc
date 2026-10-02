"""Item 68 — every ending of a run journals its open nodes as cancelled, with the reason.

The run view reads the per-node snapshot. A node left ``materialized`` or
``in_progress`` after the run ended says nothing about why it never finished,
and a direct snapshot write would be reverted by the next reconcile — so the
fact has to live in the journal that ``derive_node_states`` reads.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from arcteam.workflow.runner_state import derive_node_states

from .conftest import (
    SALES_DID,
    Budget,
    Bundle,
    Definition,
    Node,
    complete_node,
    task_id,
)
from .test_runner_frontier import FakeDefinitions, build

CHAIN = Definition(
    id="swept",
    channel="channel://swept",
    budget=Budget(tokens=100, wall_clock_s=1800),
    nodes=(
        Node(id="first", kind="agent", agent="@sales"),
        Node(id="second", kind="agent", agent="@ops", needs=("first",)),
    ),
)
_OPERATOR = "did:arc:x/1"


async def _start(stores: Any, registry: Any, **kwargs: Any) -> tuple[Any, Any]:
    runner = build(stores, registry, CHAIN, **kwargs)
    run = await runner.start_run("swept", input={}, initiator="operator", initiator_did=_OPERATOR)
    return runner, run


async def _assert_cancelled(
    stores: Any, run_id: str, node_ids: tuple[str, ...], reason_prefix: str
) -> None:
    flow_tasks, runs, _ = stores
    record = await runs.get(run_id)
    for node_id in node_ids:
        state = record.node_states[node_id]
        assert state.status == "cancelled", f"{node_id}: {state.status}"
        assert (state.reason or "").startswith(reason_prefix), state.reason
    # Reconcile re-derives from rows + journal: the snapshot must survive it.
    rows = await flow_tasks.query_by_flow_run(run_id)
    derived = derive_node_states(rows, record.path_taken)
    for node_id in node_ids:
        assert derived[node_id].status == "cancelled"


async def test_cancel_marks_every_open_node_cancelled_with_reason(
    stores: Any, registry: Any
) -> None:
    runner, run = await _start(stores, registry)

    record = await runner.cancel(run.run_id, actor_did=_OPERATOR, reason="operator pulled it")

    assert record.status == "cancelled"
    await _assert_cancelled(stores, run.run_id, ("first", "second"), "operator pulled it")


async def test_cancel_leaves_a_finished_node_done(stores: Any, registry: Any) -> None:
    _, runs, tasks = stores
    runner, run = await _start(stores, registry)
    await complete_node(tasks, task_id(run.run_id, "first", 0), SALES_DID, {"ok": True}, tokens=1)
    await runner.advance(run.run_id)

    await runner.cancel(run.run_id, actor_did=_OPERATOR)

    record = await runs.get(run.run_id)
    assert record.node_states["first"].status == "done"
    assert record.node_states["second"].status == "cancelled"


async def test_budget_exhausted_marks_open_nodes_cancelled(stores: Any, registry: Any) -> None:
    _, _, tasks = stores
    runner, run = await _start(stores, registry)

    await complete_node(
        tasks, task_id(run.run_id, "first", 0), SALES_DID, {"ok": True}, tokens=140
    )
    record = await runner.advance(run.run_id)

    assert record.status == "failed"
    await _assert_cancelled(stores, run.run_id, ("second",), "budget exhausted")


async def test_stall_marks_open_nodes_cancelled(stores: Any, registry: Any) -> None:
    flow_tasks, _, tasks = stores
    runner, run = await _start(stores, registry)

    async def swallow(tasks_: Any, *, actor_did: str, fence: Any = None) -> list[Any]:
        return []

    await complete_node(tasks, task_id(run.run_id, "first", 0), SALES_DID, {"ok": True})
    flow_tasks.create_batch = swallow  # type: ignore[method-assign]
    record = await runner.advance(run.run_id)

    assert record.status == "failed"
    await _assert_cancelled(stores, run.run_id, ("second",), "stalled")


async def test_wall_clock_budget_marks_the_in_flight_node_cancelled(
    stores: Any, registry: Any
) -> None:
    moment = datetime.now(UTC)
    runner, run = await _start(stores, registry, clock=lambda: moment)

    runner._clock = lambda: moment + timedelta(seconds=3600)
    await runner.advance(run.run_id)

    await _assert_cancelled(stores, run.run_id, ("first", "second"), "budget exhausted")


async def test_definition_change_marks_open_nodes_cancelled(stores: Any, registry: Any) -> None:
    definitions = FakeDefinitions(Bundle(CHAIN))
    runner, run = await _start(stores, registry, definitions=definitions)

    definitions._bundle = Bundle(CHAIN, content_hash="sha256:edited")
    record = await runner.advance(run.run_id)

    assert record.status == "failed"
    await _assert_cancelled(stores, run.run_id, ("first",), "definition changed under a live run")
