"""P14-B step 7 — failure propagation with reasons (on_failure policy)."""

from __future__ import annotations

from typing import Any

from .conftest import OPS_DID, SALES_DID, Definition, Node, complete_node, fail_node, task_id
from .test_runner_frontier import build

_START = {"initiator": "operator", "initiator_did": "did:arc:x/1"}


def _chain(on_failure: str) -> Definition:
    return Definition(
        id="chain3",
        nodes=(
            Node(id="a", kind="agent", agent="@sales", on_failure=on_failure),
            Node(id="b", kind="agent", agent="@ops", needs=("a",)),
            Node(id="c", kind="agent", agent="@ops", needs=("b",)),
            Node(id="solo", kind="agent", agent="@sales"),
        ),
    )


async def _fail_a_and_finish_solo(runner: Any, tasks: Any, run_id: str) -> Any:
    await fail_node(tasks, task_id(run_id, "a"), SALES_DID, "boom: upstream exploded")
    await complete_node(tasks, task_id(run_id, "solo"), SALES_DID, {"ok": True})
    return await runner.advance(run_id)


async def test_fail_run_keeps_child_last_error(stores: Any, registry: Any) -> None:
    _, runs, tasks = stores
    runner = build(stores, registry, _chain("fail_run"))
    run = await runner.start_run("chain3", input={}, **_START)

    record = await _fail_a_and_finish_solo(runner, tasks, run.run_id)

    assert record.status == "failed"
    assert "boom: upstream exploded" in (record.last_error or "")
    states = (await runs.get(run.run_id)).node_states
    assert states["a"].status == "failed"
    assert states["a"].last_error == "boom: upstream exploded"
    # Nodes that never ran say why, instead of sitting in limbo.
    assert states["b"].status == "cancelled"
    assert states["b"].reason == "upstream a failed: boom: upstream exploded"
    assert states["c"].status == "cancelled"
    assert states["c"].reason == "upstream a failed: boom: upstream exploded"


async def test_skip_dependents_marks_skipped_with_reason(stores: Any, registry: Any) -> None:
    flow_tasks, runs, tasks = stores
    runner = build(stores, registry, _chain("skip_dependents"))
    run = await runner.start_run("chain3", input={}, **_START)

    record = await _fail_a_and_finish_solo(runner, tasks, run.run_id)

    assert record.status == "done_with_failures"
    assert "boom: upstream exploded" in (record.last_error or record.resolution or "")
    states = (await runs.get(run.run_id)).node_states
    for downstream in ("b", "c"):
        assert states[downstream].status == "skipped"
        assert states[downstream].reason == "upstream a failed: boom: upstream exploded"
    assert states["solo"].status == "done", "an independent branch still finishes"
    ran = {r.metadata["node_id"] for r in await flow_tasks.query_by_flow_run(run.run_id)}
    assert ran == {"a", "solo"}


async def test_continue_marks_dependents_runnable_with_upstream_failed(
    stores: Any, registry: Any
) -> None:
    flow_tasks, runs, tasks = stores
    runner = build(stores, registry, _chain("continue"))
    run = await runner.start_run("chain3", input={}, **_START)

    record = await _fail_a_and_finish_solo(runner, tasks, run.run_id)

    assert record.status == "running", "b is runnable although a failed"
    rows = {r.metadata["node_id"]: r for r in await flow_tasks.query_by_flow_run(run.run_id)}
    assert rows["b"].metadata["upstream_failed"] == {"a": "boom: upstream exploded"}

    await complete_node(tasks, task_id(run.run_id, "b"), OPS_DID, {"x": 1})
    await runner.advance(run.run_id)
    await complete_node(tasks, task_id(run.run_id, "c"), OPS_DID, {"x": 2})
    record = await runner.advance(run.run_id)

    assert record.status == "done_with_failures"
    states = (await runs.get(run.run_id)).node_states
    assert states["a"].status == "failed"
    assert states["c"].status == "done"
