"""T-860 — resume without re-execution, and ordered cancellation.

REQ-233 (a completed node is never re-executed), REQ-234 (a restart mid-
materialization re-derives rather than duplicates), REQ-235 (cancel marks the
Run before fanning out).
"""

from __future__ import annotations

from typing import Any

import pytest
from arcstore.tasks import Task

from .conftest import OPS_DID, SALES_DID, Definition, Node, complete_node, task_id
from .test_runner_frontier import build

FANOUT = Definition(
    id="fanout",
    channel="channel://fanout",
    nodes=(
        Node(id="left", kind="agent", agent="@sales"),
        Node(id="right", kind="agent", agent="@ops"),
        Node(id="merge", kind="agent", agent="@sales", needs=("left", "right")),
    ),
)

CHAIN = Definition(
    id="chain",
    channel="channel://chain",
    nodes=(
        Node(id="first", kind="agent", agent="@sales"),
        Node(
            id="second",
            kind="tool",
            agent="@ops",
            needs=("first",),
            tool="crm_lookup",
            args={"domain": "$nodes.first.output.company_domain"},
        ),
    ),
)


async def test_restart_mid_materialization_re_derives_instead_of_duplicating(
    stores: Any, registry: Any
) -> None:
    flow_tasks, _, _ = stores
    runner = build(stores, registry, FANOUT)
    original = flow_tasks.create_batch

    async def crash_after_first(
        tasks: list[Task], *, actor_did: str, fence: Any | None = None
    ) -> list[Task]:
        await original(tasks[:1], actor_did=actor_did, fence=fence)
        raise RuntimeError("crashed mid-materialization")

    flow_tasks.create_batch = crash_after_first  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await runner.start_run(
            "fanout", input={}, initiator_did="did:arc:x/1", run_id="run-resume"
        )

    flow_tasks.create_batch = original  # type: ignore[method-assign]
    await runner.advance("run-resume")

    rows = await flow_tasks.query_by_flow_run("run-resume")
    node_ids = sorted(r.metadata["node_id"] for r in rows)
    assert node_ids == ["left", "right"], "the half-written frontier is completed, not doubled"
    assert len(flow_tasks.created_keys) == len(set(flow_tasks.created_keys))

    # The row written just before the crash was never journalled by the pass that
    # died. If resume doesn't back-fill it, the Run's path silently under-reports
    # what actually exists — and the path is supposed to BE the honest record.
    _, runs, _ = stores
    record = await runs.get("run-resume")
    journalled = sorted(
        entry["node_id"] for entry in record.path_taken if entry["kind"] == "materialized"
    )
    assert journalled == ["left", "right"]


async def test_materialization_is_idempotent_across_repeated_ticks(
    stores: Any, registry: Any
) -> None:
    flow_tasks, _, _ = stores
    runner = build(stores, registry, FANOUT)
    run = await runner.start_run("fanout", input={}, initiator_did="did:arc:x/1")

    before = list(flow_tasks.created_keys)
    for _ in range(3):
        await runner.advance(run.run_id)

    assert flow_tasks.created_keys == before
    rows = await flow_tasks.query_by_flow_run(run.run_id)
    assert len(rows) == 2
    # A materialized node is never even OFFERED to the store again: the runner
    # carries the guarantee itself rather than leaning on the store to dedupe.
    assert len(flow_tasks.requested_keys) == len(set(flow_tasks.requested_keys))


async def test_the_path_taken_records_each_node_instance_exactly_once(
    stores: Any, registry: Any
) -> None:
    """The Run's path IS the honest record — a re-tick must not inflate it."""
    _, runs, tasks = stores
    runner = build(stores, registry, CHAIN)
    run = await runner.start_run("chain", input={}, initiator_did="did:arc:x/1")

    for _ in range(3):
        await runner.advance(run.run_id)
    await complete_node(
        tasks, task_id(run.run_id, "first", 0), SALES_DID, {"company_domain": "acme.example"}
    )
    for _ in range(3):
        await runner.advance(run.run_id)

    record = await runs.get(run.run_id)
    materialized = [
        (entry["node_id"], entry["iteration"])
        for entry in record.path_taken
        if entry["kind"] == "materialized"
    ]
    assert materialized == [("first", 0), ("second", 0)]


async def test_completed_node_is_replayed_never_re_executed(stores: Any, registry: Any) -> None:
    flow_tasks, _, tasks = stores
    runner = build(stores, registry, CHAIN)
    run = await runner.start_run("chain", input={}, initiator_did="did:arc:x/1")

    first_id = task_id(run.run_id, "first", 0)
    await complete_node(tasks, first_id, SALES_DID, {"company_domain": "acme.example"})
    await runner.advance(run.run_id)
    completed_at = (await tasks.get(first_id)).completed_at

    for _ in range(3):
        await runner.advance(run.run_id)

    replayed = await tasks.get(first_id)
    assert replayed.status == "done"
    assert replayed.completed_at == completed_at, "a done node is never touched again"
    assert flow_tasks.created_keys.count(first_id) == 1

    rows = {r.metadata["node_id"]: r for r in await flow_tasks.query_by_flow_run(run.run_id)}
    assert rows["second"].metadata["args"] == {"domain": "acme.example"}


async def test_cancel_marks_the_run_before_fanning_out_node_cancels(
    stores: Any, registry: Any
) -> None:
    flow_tasks, runs, _ = stores
    runner = build(stores, registry, FANOUT)
    run = await runner.start_run("fanout", input={}, initiator_did="did:arc:x/1")

    async def observe() -> str | None:
        record = await runs.get(run.run_id)
        return None if record is None else record.status

    flow_tasks.observe_run_status = observe

    await runner.cancel(run.run_id, actor_did="did:arc:local:user/9", reason="operator stop")

    assert flow_tasks.run_status_at_cancel, "the sweep must actually touch the node rows"
    assert set(flow_tasks.run_status_at_cancel) == {"cancelled"}
    record = await runs.get(run.run_id)
    assert record.status == "cancelled"
    assert record.resolution == "operator stop"


async def test_a_node_completing_during_the_sweep_cannot_extend_the_frontier(
    stores: Any, registry: Any
) -> None:
    flow_tasks, runs, tasks = stores
    runner = build(stores, registry, CHAIN)
    run = await runner.start_run("chain", input={}, initiator_did="did:arc:x/1")

    await runner.cancel(run.run_id, actor_did="did:arc:local:user/9", reason="operator stop")
    await complete_node(
        tasks, task_id(run.run_id, "first", 0), SALES_DID, {"company_domain": "acme.example"}
    )
    record = await runner.advance(run.run_id)

    assert record.status == "cancelled"
    node_ids = {r.metadata["node_id"] for r in await flow_tasks.query_by_flow_run(run.run_id)}
    assert node_ids == {"first"}, "the frontier is closed the moment the Run row flips"


async def test_cancel_of_a_terminal_run_is_refused(stores: Any, registry: Any) -> None:
    _, runs, tasks = stores
    runner = build(stores, registry, CHAIN)
    run = await runner.start_run("chain", input={}, initiator_did="did:arc:x/1")
    await complete_node(
        tasks, task_id(run.run_id, "first", 0), SALES_DID, {"company_domain": "acme.example"}
    )
    await runner.advance(run.run_id)
    await complete_node(tasks, task_id(run.run_id, "second", 0), OPS_DID, {"ok": True})
    record = await runner.advance(run.run_id)
    assert record.status == "done"

    after = await runner.cancel(run.run_id, actor_did="did:arc:local:user/9", reason="too late")
    assert after.status == "done"


async def test_a_terminating_tick_loses_a_race_with_a_cancel(stores: Any, registry: Any) -> None:
    """The roll-up write is conditional, so a cancel landing first is not undone.

    Same invariant as the cancel ordering, at the other seam: the tick is holding
    a run it read as `running`, and an operator cancels in the window before its
    write lands. An unconditional write would flip a cancelled run to `done`.
    """
    flow_tasks, runs, tasks = stores
    runner = build(stores, registry, CHAIN)
    run = await runner.start_run("chain", input={}, initiator_did="did:arc:x/1")
    await complete_node(
        tasks, task_id(run.run_id, "first", 0), SALES_DID, {"company_domain": "acme.example"}
    )
    await runner.advance(run.run_id)
    await complete_node(tasks, task_id(run.run_id, "second", 0), OPS_DID, {"ok": True})

    original = runs.set_status

    async def operator_cancels_first(
        run_id: str,
        status: str,
        *,
            actor_did: str,
            expected_status: str | None = None,
            resolution: str | None = None,
            fence: Any | None = None,
    ) -> bool:
        if status == "done":
            await original(
                run_id,
                "cancelled",
                    actor_did="did:arc:local:user/9",
                    resolution="operator stop",
                    fence=None,
            )
        return await original(
            run_id,
            status,
            actor_did=actor_did,
                expected_status=expected_status,
                resolution=resolution,
                fence=fence,
        )

    runs.set_status = operator_cancels_first  # type: ignore[method-assign]
    record = await runner.advance(run.run_id)

    assert record.status == "cancelled", "a completed tick must not resurrect a cancelled run"
    assert record.resolution == "operator stop"


async def test_one_failing_node_cancel_does_not_abort_the_sweep(
    stores: Any, registry: Any
) -> None:
    """Per-step try/except on cleanup: one bad row must not strand the others."""
    flow_tasks, _, _ = stores
    runner = build(stores, registry, FANOUT)
    run = await runner.start_run("fanout", input={}, initiator_did="did:arc:x/1")
    original = flow_tasks.request_cancel
    seen: list[str] = []

    async def flaky(task_id: str, *, actor_did: str) -> Any:
        seen.append(task_id)
        if len(seen) == 1:
            raise RuntimeError("row exploded")
        return await original(task_id, actor_did=actor_did)

    flow_tasks.request_cancel = flaky  # type: ignore[method-assign]
    await runner.cancel(run.run_id, actor_did="did:arc:local:user/9", reason="stop")

    assert len(seen) == 2, "the second row is still swept after the first one raises"
