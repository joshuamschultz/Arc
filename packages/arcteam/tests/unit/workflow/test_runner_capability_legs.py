"""T-865 — the run's capability legs travel node to node (COMP-015, REQ-236).

COMP-015's carrier closes the session boundary *inside* one node's dispatch, and
the node writes back what it lit. That only stops the laundering attack if the
runner then STAMPS the run's accumulation onto the next node it materializes:
without that stamp every node starts from an empty set, the carrier is seeded
with nothing, and a definition whose first node reads private data and whose
second node talks outward completes a composition no single session could.

These tests drive the real runner over real task rows, so a stamp that never
reaches the row fails them.
"""

from __future__ import annotations

from typing import Any

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


class Definitions:
    def __init__(self, bundle: Bundle) -> None:
        self._bundle = bundle

    def load(self, workflow_id: str) -> Bundle:
        return self._bundle

    def load_for_run(self, workflow_id: str) -> Bundle:
        return self._bundle

    def load_for_dispatch(self, workflow_id: str) -> Bundle:
        return self._bundle


def two_node_chain() -> Definition:
    return Definition(
        id="chain",
        owner="@sales",
        nodes=(
            Node(id="read", kind="agent", agent="@sales"),
            Node(id="send", kind="agent", agent="@sales", needs=("read",)),
        ),
    )


def build(stores: Any, registry: Any, definition: Definition, **kwargs: Any) -> WorkflowRunner:
    flow_tasks, runs, _ = stores
    return WorkflowRunner(
        tasks=flow_tasks,
        runs=runs,
        definitions=Definitions(Bundle(definition)),
        owners=registry,
        runner_did=RUNNER_DID,
        tier="personal",
        evaluate=evaluate,
        resolve_args=resolve_args,
        **kwargs,
    )


async def light_legs(tasks: Any, row_id: str, legs: list[str]) -> None:
    """Record on a completed row what its dispatch lit, as the node adapter does."""
    current = await tasks.get(row_id)
    assert current is not None
    metadata = {**current.metadata, "accumulated_legs": sorted(legs)}
    await tasks.update(row_id, {"metadata": metadata}, actor_did=SALES_DID)


async def test_next_node_is_stamped_with_what_the_previous_node_lit(
    stores: Any, registry: Any
) -> None:
    flow_tasks, _, tasks = stores
    runner = build(stores, registry, two_node_chain())
    run = await runner.start_run("chain", input={}, initiator_did="did:arc:local:user/1")

    await complete_node(tasks, task_id(run.run_id, "read", 0), SALES_DID, {"ok": True})
    await light_legs(tasks, task_id(run.run_id, "read", 0), ["private_data"])
    await runner.advance(run.run_id)

    rows = {r.metadata["node_id"]: r for r in await flow_tasks.query_by_flow_run(run.run_id)}
    assert rows["send"].metadata["accumulated_legs"] == ["private_data"]


async def test_a_node_that_lit_nothing_stamps_an_empty_set(stores: Any, registry: Any) -> None:
    """Threading must not invent legs: an empty run stays empty and unblocked."""
    flow_tasks, _, tasks = stores
    runner = build(stores, registry, two_node_chain())
    run = await runner.start_run("chain", input={}, initiator_did="did:arc:local:user/1")

    await complete_node(tasks, task_id(run.run_id, "read", 0), SALES_DID, {"ok": True})
    await runner.advance(run.run_id)

    rows = {r.metadata["node_id"]: r for r in await flow_tasks.query_by_flow_run(run.run_id)}
    assert rows["send"].metadata["accumulated_legs"] == []


async def test_accumulation_unions_across_every_completed_node(stores: Any, registry: Any) -> None:
    """A parallel branch cannot launder a leg: the union is run-scoped."""
    definition = Definition(
        id="fan",
        owner="@sales",
        nodes=(
            Node(id="a", kind="agent", agent="@sales"),
            Node(id="b", kind="agent", agent="@sales"),
            Node(id="c", kind="agent", agent="@sales", needs=("a", "b")),
        ),
    )
    flow_tasks, _, tasks = stores
    runner = build(stores, registry, definition)
    run = await runner.start_run("fan", input={}, initiator_did="did:arc:local:user/1")

    for node_id, leg in (("a", "private_data"), ("b", "untrusted_input")):
        await complete_node(tasks, task_id(run.run_id, node_id, 0), SALES_DID, {})
        await light_legs(tasks, task_id(run.run_id, node_id, 0), [leg])
    await runner.advance(run.run_id)

    rows = {r.metadata["node_id"]: r for r in await flow_tasks.query_by_flow_run(run.run_id)}
    assert rows["c"].metadata["accumulated_legs"] == ["private_data", "untrusted_input"]


async def test_accumulation_is_bounded_and_the_truncation_is_audited(
    stores: Any, registry: Any
) -> None:
    """A per-run security collection needs a ceiling, and a silent one is useless."""
    flow_tasks, _, tasks = stores
    sink = RecordingSink()
    runner = build(stores, registry, two_node_chain(), audit_sink=sink, max_capability_legs=2)
    run = await runner.start_run("chain", input={}, initiator_did="did:arc:local:user/1")

    await complete_node(tasks, task_id(run.run_id, "read", 0), SALES_DID, {})
    await light_legs(tasks, task_id(run.run_id, "read", 0), ["c", "a", "d", "b"])
    await runner.advance(run.run_id)

    rows = {r.metadata["node_id"]: r for r in await flow_tasks.query_by_flow_run(run.run_id)}
    assert rows["send"].metadata["accumulated_legs"] == ["a", "b"]
    assert "workflow.legs.truncated" in sink.actions()


async def test_materialization_audit_records_the_legs_the_node_starts_with(
    stores: Any, registry: Any
) -> None:
    """Growth is observable: the record names what each node was pre-charged with."""
    flow_tasks, _, tasks = stores
    sink = RecordingSink()
    runner = build(stores, registry, two_node_chain(), audit_sink=sink)
    run = await runner.start_run("chain", input={}, initiator_did="did:arc:local:user/1")

    await complete_node(tasks, task_id(run.run_id, "read", 0), SALES_DID, {})
    await light_legs(tasks, task_id(run.run_id, "read", 0), ["private_data"])
    await runner.advance(run.run_id)

    materialized = [
        event
        for event in sink.events
        if event.action == "workflow.node.materialized" and event.target.endswith("/send")
    ]
    assert materialized
    assert materialized[-1].extra["accumulated_legs"] == ["private_data"]
