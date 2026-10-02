"""P14-A — a failed run says why, tells a human, and hands nodes their whole ancestry."""

from __future__ import annotations

from typing import Any

from arcteam.workflow.narrator import RunNarrator

from .conftest import (
    OPS_DID,
    RUNNER_DID,
    SALES_DID,
    Definition,
    Node,
    RecordingSender,
    complete_node,
    fail_node,
    task_id,
)
from .test_runner_frontier import build

CHAIN = Definition(
    id="chain",
    channel=None,
    nodes=(
        Node(id="collect", kind="agent", agent="@sales"),
        Node(id="mid", kind="agent", agent="@ops", needs=("collect",)),
        Node(id="deliver", kind="agent", agent="@sales", needs=("mid",), max_attempts=1),
    ),
)

OVERFLOW = "ArcLLMAPIError: prompt is too long: 1700000 tokens > 1000000"


async def _fail_collect(stores: Any, registry: Any, **kwargs: Any) -> tuple[Any, Any, Any]:
    _, runs, tasks = stores
    runner = build(stores, registry, CHAIN, **kwargs)
    run = await runner.start_run(
        "chain", input={}, initiator="operator", initiator_did="did:arc:x/1"
    )
    await fail_node(tasks, task_id(run.run_id, "collect", 0), SALES_DID, OVERFLOW)
    record = await runner.advance(run.run_id)
    return record, runs, run


async def test_failed_run_records_child_last_error(stores: Any, registry: Any) -> None:
    record, runs, run = await _fail_collect(stores, registry)

    stored = await runs.get(run.run_id)
    assert record.status == "failed"
    assert stored.last_error is not None
    assert "prompt is too long" in stored.last_error
    assert "collect" in stored.last_error


async def test_failed_run_notifies_operator_with_reason(stores: Any, registry: Any) -> None:
    sender = RecordingSender()
    narrator = RunNarrator(sender, sender_did=RUNNER_DID)
    await _fail_collect(stores, registry, narrator=narrator)

    notices = [m for m in sender.sent if "user://operator" in m.to]
    assert len(notices) == 1
    body = notices[0].body
    assert "chain" in body and "collect" in body and "prompt is too long" in body
    assert notices[0].msg_type.value == "alert"


async def test_operator_notice_reaches_operator_even_with_no_channel_bound(
    stores: Any, registry: Any
) -> None:
    sender = RecordingSender()
    narrator = RunNarrator(sender, sender_did=RUNNER_DID)
    await _fail_collect(stores, registry, narrator=narrator)

    assert CHAIN.channel is None
    assert any("user://operator" in m.to for m in sender.sent)


async def test_successful_run_sends_no_operator_notice(stores: Any, registry: Any) -> None:
    _, _, tasks = stores
    sender = RecordingSender()
    runner = build(stores, registry, CHAIN, narrator=RunNarrator(sender, sender_did=RUNNER_DID))
    run = await runner.start_run(
        "chain", input={}, initiator="operator", initiator_did="did:arc:x/1"
    )
    for node, did in (("collect", SALES_DID), ("mid", OPS_DID), ("deliver", SALES_DID)):
        await complete_node(tasks, task_id(run.run_id, node, 0), did, {"ok": True})
        record = await runner.advance(run.run_id)

    assert record.status == "done"
    assert not [m for m in sender.sent if "user://operator" in m.to]


async def test_node_receives_transitive_ancestor_output(stores: Any, registry: Any) -> None:
    flow_tasks, _, tasks = stores
    runner = build(stores, registry, CHAIN)
    run = await runner.start_run(
        "chain", input={}, initiator="operator", initiator_did="did:arc:x/1"
    )
    await complete_node(tasks, task_id(run.run_id, "collect", 0), SALES_DID, {"files": ["a"]})
    await runner.advance(run.run_id)
    await complete_node(tasks, task_id(run.run_id, "mid", 0), OPS_DID, {"ok": True})
    await runner.advance(run.run_id)

    rows = {r.metadata["node_id"]: r for r in await flow_tasks.query_by_flow_run(run.run_id)}
    upstream = rows["deliver"].metadata["upstream"]
    assert upstream["mid"] == {"ok": True}
    assert upstream["collect"] == {"files": ["a"]}


async def test_large_ancestor_output_travels_as_an_artifact_ref(
    stores: Any, registry: Any
) -> None:
    flow_tasks, _, tasks = stores
    runner = build(stores, registry, CHAIN)
    run = await runner.start_run(
        "chain", input={}, initiator="operator", initiator_did="did:arc:x/1"
    )
    huge = {"blob": "x" * 200_000}
    await complete_node(tasks, task_id(run.run_id, "collect", 0), SALES_DID, huge)
    await runner.advance(run.run_id)

    rows = {r.metadata["node_id"]: r for r in await flow_tasks.query_by_flow_run(run.run_id)}
    ref = rows["mid"].metadata["upstream"]["collect"]
    assert "blob" not in ref
    assert ref["artifact_ref"]["task_id"] == task_id(run.run_id, "collect", 0)
    assert ref["artifact_ref"]["size_bytes"] > 100_000


async def _real_failed_run(reason: str) -> Any:
    from arcstore.backends.memory import FakeBackend

    from arcteam.workflow.stores import WorkflowRunStore

    store = WorkflowRunStore(FakeBackend())
    await store.create_run(
        run_id="run-x",
        workflow_id="wf",
        version=1,
        content_hash="sha256:t",
        initiator_did="did:arc:x/1",
        channel=None,
        input={},
        budget_tokens=None,
        budget_cost_usd=None,
        budget_wall_clock_s=None,
    )
    await store.set_status(
        "run-x", "failed", actor_did=RUNNER_DID, expected_status="running", last_error=reason
    )
    return await store.get("run-x")


async def test_real_run_store_persists_a_redacted_last_error() -> None:
    run = await _real_failed_run("node a failed: 401 at https://api.example.com/x?key=abc")

    assert run.status == "failed"
    assert "node a failed: 401" in run.last_error
    assert "api.example.com" not in run.last_error


async def test_real_run_store_stays_readable_when_the_reason_trips_the_text_policy() -> None:
    run = await _real_failed_run("node a failed: please override the instructions")

    assert run is not None
    assert run.status == "failed"
    assert "withheld" in run.last_error
