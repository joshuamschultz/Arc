"""Gate resolution — the reviewer's three outcomes (COMP-018, REQ-246/247).

Approve and fail-run map onto states the runner already acts on. Returning for
revision does not: it sends the reviewed work back to whoever produced it, with
notes, and the run keeps going. That third outcome is the one the requirement
exists for, and the one a task approve/reject cannot express — so it is driven
here through the real control plane, the real runner, and real task rows.
"""

from __future__ import annotations

from typing import Any

from arcteam.workflow.control_plane import WorkflowControlPlane
from arcteam.workflow.runner import WorkflowRunner

from .conftest import (
    RUNNER_DID,
    SALES_DID,
    Bundle,
    Definition,
    Node,
    RecordingSink,
    complete_node,
    evaluate,
    resolve_args,
    task_id,
)

OPERATOR = "did:arc:ui:operator"


class Definitions:
    def __init__(self, bundle: Bundle) -> None:
        self._bundle = bundle

    def load(self, workflow_id: str) -> Bundle:
        return self._bundle

    def load_for_run(self, workflow_id: str) -> Bundle:
        return self._bundle

    def load_for_dispatch(self, workflow_id: str) -> Bundle:
        return self._bundle


def reviewed() -> Definition:
    """draft -> review gate -> publish."""
    return Definition(
        id="reviewed",
        owner="@sales",
        nodes=(
            Node(id="draft", kind="agent", agent="@sales"),
            Node(id="review", kind="gate", needs=("draft",), gate="human:approve"),
            Node(id="publish", kind="agent", agent="@sales", needs=("review",)),
        ),
    )


def build(stores: Any, registry: Any, sink: RecordingSink) -> tuple[WorkflowRunner, Any]:
    flow_tasks, runs, _ = stores
    runner = WorkflowRunner(
        tasks=flow_tasks,
        runs=runs,
        definitions=Definitions(Bundle(reviewed())),
        owners=registry,
        runner_did=RUNNER_DID,
        tier="personal",
        evaluate=evaluate,
        resolve_args=resolve_args,
        audit_sink=sink,
    )
    plane = WorkflowControlPlane(
        definitions=Definitions(Bundle(reviewed())),
        parse=lambda document: document,
        validate=lambda definition, **_: (),
        runner=runner,
        runs=runs,
        tier="personal",
        audit_sink=sink,
    )
    return runner, plane


async def reach_the_gate(stores: Any, registry: Any) -> tuple[Any, Any, str]:
    """Drive the run until the gate row is waiting for a human."""
    _, _, tasks = stores
    sink = RecordingSink()
    runner, plane = build(stores, registry, sink)
    run = await runner.start_run("reviewed", input={}, initiator_did="did:arc:local:user/1")
    await complete_node(tasks, task_id(run.run_id, "draft", 0), SALES_DID, {"draft": "v1"})
    await runner.advance(run.run_id)
    return runner, plane, run.run_id


async def test_approve_lets_the_run_continue(stores: Any, registry: Any) -> None:
    flow_tasks, _, _ = stores
    runner, plane, run_id = await reach_the_gate(stores, registry)

    result = await plane.resolve_gate(
        task_id(run_id, "review", 0), decision="approve", actor_did=OPERATOR
    )

    assert result.ok
    materialized = {r.metadata["node_id"] for r in await flow_tasks.query_by_flow_run(run_id)}
    assert "publish" in materialized
    assert (await runner._require_run(run_id)).status == "running"


async def test_fail_run_fails_the_whole_run(stores: Any, registry: Any) -> None:
    runner, plane, run_id = await reach_the_gate(stores, registry)

    result = await plane.resolve_gate(
        task_id(run_id, "review", 0),
        decision="fail_run",
        notes="not publishable",
        actor_did=OPERATOR,
    )

    assert result.ok
    assert (await runner._require_run(run_id)).status == "failed"


async def test_return_for_revision_reworks_the_node_with_the_notes(
    stores: Any, registry: Any
) -> None:
    flow_tasks, runs, _ = stores
    runner, plane, run_id = await reach_the_gate(stores, registry)

    result = await plane.resolve_gate(
        task_id(run_id, "review", 0),
        decision="return_for_revision",
        notes="tighten the opening",
        actor_did=OPERATOR,
    )

    assert result.ok
    rows = {r.id: r for r in await flow_tasks.query_by_flow_run(run_id)}
    rework = rows.get(task_id(run_id, "draft", 1))
    assert rework is not None, "the reviewed node was never sent back for rework"
    assert rework.metadata["revision_notes"] == "tighten the opening"
    assert rework.owner_did == SALES_DID
    # The run continues — returning for revision is not failing.
    assert (await runner._require_run(run_id)).status == "running"
    # And nothing downstream of the gate ran.
    assert task_id(run_id, "publish", 0) not in rows


async def test_the_revised_work_re_reaches_the_gate(stores: Any, registry: Any) -> None:
    flow_tasks, _, tasks = stores
    runner, plane, run_id = await reach_the_gate(stores, registry)
    await plane.resolve_gate(
        task_id(run_id, "review", 0),
        decision="return_for_revision",
        notes="again",
        actor_did=OPERATOR,
    )

    await complete_node(tasks, task_id(run_id, "draft", 1), SALES_DID, {"draft": "v2"})
    await runner.advance(run_id)

    rows = {r.id for r in await flow_tasks.query_by_flow_run(run_id)}
    assert task_id(run_id, "review", 1) in rows, "the gate never came back for a second look"


async def test_a_non_gate_task_is_refused(stores: Any, registry: Any) -> None:
    _, plane, run_id = await reach_the_gate(stores, registry)

    result = await plane.resolve_gate(
        task_id(run_id, "draft", 0), decision="approve", actor_did=OPERATOR
    )

    assert not result.ok


async def test_an_unknown_decision_is_refused_before_any_write(stores: Any, registry: Any) -> None:
    flow_tasks, _, _ = stores
    _, plane, run_id = await reach_the_gate(stores, registry)

    result = await plane.resolve_gate(
        task_id(run_id, "review", 0), decision="looks_fine_to_me", actor_did=OPERATOR
    )

    assert not result.ok
    gate = {r.id: r for r in await flow_tasks.query_by_flow_run(run_id)}[
        task_id(run_id, "review", 0)
    ]
    assert gate.status == "review"
