"""P14-B step 8 — retry one failed node without re-running what already finished (J3 G3)."""

from __future__ import annotations

from typing import Any

from arcteam.workflow.control_plane import WorkflowControlPlane

from .conftest import (
    OPS_DID,
    SALES_DID,
    Bundle,
    Definition,
    Node,
    RecordingSink,
    complete_node,
    fail_node,
    task_id,
)
from .test_control_plane import FakeDefinitionStore, parse, validate
from .test_runner_frontier import build

OPERATOR = "did:arc:local:user/0f0f0f0f"

DAG = Definition(
    id="dag3",
    nodes=(
        Node(id="a", kind="agent", agent="@sales"),
        Node(id="b", kind="agent", agent="@ops", needs=("a",)),
        Node(id="c", kind="agent", agent="@ops", needs=("b",)),
    ),
)


def _plane(stores: Any, registry: Any) -> tuple[WorkflowControlPlane, Any]:
    definitions = FakeDefinitionStore()
    definitions.bundles["dag3"] = Bundle(DAG)
    sink = RecordingSink()
    runner = build(stores, registry, DAG, definitions=definitions)
    control = WorkflowControlPlane(
        definitions=definitions,
        parse=parse,
        validate=validate,
        runner=runner,
        runs=stores[1],
        tier="personal",
        audit_sink=sink,
    )
    return control, sink


async def _failed_run(control: WorkflowControlPlane, stores: Any) -> str:
    _, _, tasks = stores
    started = await control.run("dag3", input={}, initiator="operator", actor_did=OPERATOR)
    run_id = started.run.run_id
    await complete_node(tasks, task_id(run_id, "a"), SALES_DID, {"v": 1})
    await control.runner.advance(run_id)
    await fail_node(tasks, task_id(run_id, "b"), OPS_DID, "dead-lettered: timeout")
    record = await control.runner.advance(run_id)
    assert record.status == "failed"
    return run_id


async def test_retry_failed_node_skips_completed_upstream(stores: Any, registry: Any) -> None:
    """J3 G3: the retry re-runs the failed node only; node `a` never executes again."""
    flow_tasks, runs, tasks = stores
    control, sink = _plane(stores, registry)
    run_id = await _failed_run(control, stores)
    executions_of_a = [k for k in flow_tasks.created_keys if k == task_id(run_id, "a")]

    result = await control.retry_node(run_id, "b", actor_did=OPERATOR)

    assert result.ok, result.errors
    assert result.run.status == "running"
    assert (await runs.get(run_id)).last_error in (None, "")
    assert flow_tasks.created_keys.count(task_id(run_id, "a")) == len(executions_of_a) == 1
    retried = await tasks.get(task_id(run_id, "b", 1))
    assert retried is not None and retried.attempts == 0
    assert "workflow.node.retried" in sink.actions()

    await complete_node(tasks, task_id(run_id, "b", 1), OPS_DID, {"v": 2})
    await control.runner.advance(run_id)
    await complete_node(tasks, task_id(run_id, "c", 1), OPS_DID, {"v": 3})
    record = await control.runner.advance(run_id)

    assert record.status == "done"
    states = (await runs.get(run_id)).node_states
    assert {k: v.status for k, v in states.items()} == {"a": "done", "b": "done", "c": "done"}
    assert flow_tasks.created_keys.count(task_id(run_id, "a")) == 1, "a ran exactly once"


async def test_retry_refused_on_running_run(stores: Any, registry: Any) -> None:
    control, sink = _plane(stores, registry)
    started = await control.run("dag3", input={}, initiator="operator", actor_did=OPERATOR)

    result = await control.retry_node(started.run.run_id, "a", actor_did=OPERATOR)

    assert not result.ok
    assert "only a failed run" in result.errors[0].error
    assert [e.outcome for e in sink.events if e.action == "workflow.node.retried"] == ["refused"]


async def test_retry_refused_for_a_node_that_did_not_fail(stores: Any, registry: Any) -> None:
    control, _ = _plane(stores, registry)
    run_id = await _failed_run(control, stores)

    done_node = await control.retry_node(run_id, "a", actor_did=OPERATOR)
    never_ran = await control.retry_node(run_id, "c", actor_did=OPERATOR)
    unknown = await control.retry_node(run_id, "ghost", actor_did=OPERATOR)

    assert not done_node.ok and not never_ran.ok and not unknown.ok


async def test_two_retries_of_the_same_failure_create_one_attempt(
    stores: Any, registry: Any
) -> None:
    flow_tasks, _, _ = stores
    control, _ = _plane(stores, registry)
    run_id = await _failed_run(control, stores)

    first = await control.retry_node(run_id, "b", actor_did=OPERATOR)
    second = await control.retry_node(run_id, "b", actor_did=OPERATOR)

    assert first.ok and not second.ok
    assert flow_tasks.created_keys.count(task_id(run_id, "b", 1)) == 1
