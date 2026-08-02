"""T-866 / T-867 — the handoff IS the task write (D-538, REQ-251).

The load-bearing test in this suite is the first one: with the messenger
stubbed to drop every single send, a multi-agent run must still advance node to
node and reach a terminal state. If that ever fails, the design is violated.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

from arcteam.workflow.narrator import RunNarrator
from arcteam.workflow.runner import WorkflowRunner

from .conftest import (
    OPS_DID,
    REVIEWER_DID,
    RUNNER_DID,
    SALES_DID,
    Definition,
    DroppingSender,
    Node,
    complete_node,
    task_id,
)
from .test_runner_frontier import build

THREE_AGENTS = Definition(
    id="handoff",
    channel="channel://handoff",
    nodes=(
        Node(id="collect", kind="agent", agent="@sales"),
        Node(
            id="build",
            kind="tool",
            agent="@ops",
            needs=("collect",),
            tool="crm_lookup",
            args={"domain": "$nodes.collect.output.company_domain"},
        ),
        Node(id="review", kind="agent", agent="@reviewer", needs=("build",)),
    ),
)


async def test_a_run_completes_with_every_outbound_message_dropped(
    stores: Any, registry: Any
) -> None:
    sender = DroppingSender()
    narrator = RunNarrator(sender, sender_did=RUNNER_DID)
    _, runs, tasks = stores
    runner = build(stores, registry, THREE_AGENTS, narrator=narrator)

    run = await runner.start_run("handoff", input={}, initiator_did="did:arc:x/1")
    await complete_node(
        tasks, task_id(run.run_id, "collect", 0), SALES_DID, {"company_domain": "acme.example"}
    )
    await runner.advance(run.run_id)
    await complete_node(tasks, task_id(run.run_id, "build", 0), OPS_DID, {"record_id": 17})
    await runner.advance(run.run_id)
    await complete_node(tasks, task_id(run.run_id, "review", 0), REVIEWER_DID, {"verdict": "ok"})
    record = await runner.advance(run.run_id)

    assert sender.attempts > 0, "narration was genuinely attempted and genuinely dropped"
    assert record.status == "done"


async def test_every_row_carries_exactly_the_owner_named_by_the_definition(
    stores: Any, registry: Any
) -> None:
    flow_tasks, _, tasks = stores
    runner = build(
        stores,
        registry,
        THREE_AGENTS,
        narrator=RunNarrator(DroppingSender(), sender_did=RUNNER_DID),
    )
    run = await runner.start_run("handoff", input={}, initiator_did="did:arc:x/1")

    await complete_node(
        tasks, task_id(run.run_id, "collect", 0), SALES_DID, {"company_domain": "acme.example"}
    )
    await runner.advance(run.run_id)
    await complete_node(tasks, task_id(run.run_id, "build", 0), OPS_DID, {"record_id": 17})
    await runner.advance(run.run_id)

    owners = {
        r.metadata["node_id"]: r.owner_did for r in await flow_tasks.query_by_flow_run(run.run_id)
    }
    assert owners == {"collect": SALES_DID, "build": OPS_DID, "review": REVIEWER_DID}


async def test_two_agents_racing_one_row_produce_exactly_one_claim(
    stores: Any, registry: Any
) -> None:
    _, _, tasks = stores
    runner = build(stores, registry, THREE_AGENTS)
    run = await runner.start_run("handoff", input={}, initiator_did="did:arc:x/1")
    collect_id = task_id(run.run_id, "collect", 0)

    # The row names one owner, so a foreign agent cannot take it at all.
    _, foreign = await tasks.start_task(collect_id, OPS_DID)
    assert foreign == "no_tasks_available"

    # Two dispatch loops of the OWNING agent racing the same row: one claim.
    first, second = await asyncio.gather(
        tasks.start_task(collect_id, SALES_DID),
        tasks.start_task(collect_id, SALES_DID),
    )
    winners = [outcome for _, outcome in (first, second) if outcome == "assigned"]
    assert len(winners) == 1, f"exactly one claim must win, got {first} / {second}"


async def test_a_nodes_inputs_come_from_task_rows_never_from_a_message_body(
    stores: Any, registry: Any
) -> None:
    """The wiring the next node executes on is written INTO its row."""
    flow_tasks, _, tasks = stores
    runner = build(
        stores,
        registry,
        THREE_AGENTS,
        narrator=RunNarrator(DroppingSender(), sender_did=RUNNER_DID),
    )
    run = await runner.start_run("handoff", input={}, initiator_did="did:arc:x/1")

    await complete_node(
        tasks, task_id(run.run_id, "collect", 0), SALES_DID, {"company_domain": "acme.example"}
    )
    await runner.advance(run.run_id)

    rows = {r.metadata["node_id"]: r for r in await flow_tasks.query_by_flow_run(run.run_id)}
    assert rows["build"].metadata["args"] == {"domain": "acme.example"}
    assert rows["build"].metadata["upstream"] == {"collect": {"company_domain": "acme.example"}}


def test_the_runner_has_no_inbound_message_path_at_all() -> None:
    """Nothing the runner does can depend on reading a message."""
    source = Path(inspect.getfile(WorkflowRunner)).read_text()
    for forbidden in (".poll(", ".receive(", ".subscribe(", "poll_all", "get_thread"):
        assert forbidden not in source, f"runner must not read messages ({forbidden})"
    params = inspect.signature(WorkflowRunner.__init__).parameters
    assert "messenger" not in params, "the runner narrates through a narrator, one-way"
