"""T-861 — run budget (reserve-then-settle), stall detection, terminal roll-up.

REQ-228, REQ-236.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from arcteam.workflow.runner_budget import RunBudget

from .conftest import OPS_DID, SALES_DID, Budget, Definition, Node, complete_node, fail_node
from .test_runner_frontier import build

CHAIN = Definition(
    id="budgeted",
    channel="channel://budgeted",
    budget=Budget(tokens=100, wall_clock_s=1800),
    nodes=(
        Node(id="first", kind="agent", agent="@sales"),
        Node(id="second", kind="agent", agent="@ops", needs=("first",)),
    ),
)


# ---------------------------------------------------------------------------
# The accounting itself (ported from planning/executor.py:212-238)
# ---------------------------------------------------------------------------


async def test_reserve_subtracts_from_what_a_later_reserve_can_see() -> None:
    budget = RunBudget(max_tokens=100, max_cost_usd=None)

    first = await budget.reserve(per_node_tokens=60, per_node_cost=None)
    second = await budget.reserve(per_node_tokens=60, per_node_cost=None)

    assert first == (60, None)
    assert second == (40, None), "the second branch sees the first branch's reservation"
    assert await budget.reserve(per_node_tokens=60, per_node_cost=None) is None


async def test_settle_accrues_actual_spend_and_frees_the_reservation() -> None:
    budget = RunBudget(max_tokens=100, max_cost_usd=10.0)
    grant = await budget.reserve(per_node_tokens=60, per_node_cost=6.0)
    assert grant is not None

    await budget.settle(grant, tokens_used=10, cost_usd=1.0)

    assert budget.tokens_spent == 10
    assert budget.reserved_tokens == 0
    assert budget.available_budget() == (90, 9.0)


async def test_an_unbounded_dimension_never_blocks_admission() -> None:
    budget = RunBudget(max_tokens=None, max_cost_usd=None)
    assert await budget.reserve(per_node_tokens=None, per_node_cost=None) == (None, None)
    assert budget.available_budget() == (None, None)


# ---------------------------------------------------------------------------
# The run-level enforcement
# ---------------------------------------------------------------------------


async def test_run_terminates_when_the_token_budget_is_exhausted(
    stores: Any, registry: Any
) -> None:
    _, runs, tasks = stores
    runner = build(stores, registry, CHAIN)
    run = await runner.start_run("budgeted", input={}, initiator_did="did:arc:x/1")

    await complete_node(tasks, f"wf-{run.run_id}-first-0", SALES_DID, {"ok": True}, tokens=140)
    record = await runner.advance(run.run_id)

    assert record.status == "failed"
    assert "budget" in (record.resolution or "")
    assert record.tokens_spent == 140


async def test_spend_is_settled_once_per_node_not_once_per_tick(
    stores: Any, registry: Any
) -> None:
    _, runs, tasks = stores
    runner = build(stores, registry, CHAIN)
    run = await runner.start_run("budgeted", input={}, initiator_did="did:arc:x/1")

    await complete_node(tasks, f"wf-{run.run_id}-first-0", SALES_DID, {"ok": True}, tokens=20)
    for _ in range(3):
        await runner.advance(run.run_id)

    record = await runs.get(run.run_id)
    assert record.tokens_spent == 20


async def test_run_terminates_when_the_wall_clock_budget_is_exhausted(
    stores: Any, registry: Any
) -> None:
    moment = datetime.now(UTC)
    definition = Definition(
        id="budgeted",
        budget=Budget(tokens=None, wall_clock_s=10),
        nodes=(Node(id="first", kind="agent", agent="@sales"),),
    )
    _, runs, _ = stores
    runner = build(stores, registry, definition, clock=lambda: moment)
    run = await runner.start_run("budgeted", input={}, initiator_did="did:arc:x/1")

    runner._clock = lambda: moment + timedelta(seconds=30)
    record = await runner.advance(run.run_id)

    assert record.status == "failed"
    assert "wall clock" in (record.resolution or "")


async def test_a_failed_node_rolls_the_run_into_failed(stores: Any, registry: Any) -> None:
    _, runs, tasks = stores
    runner = build(stores, registry, CHAIN)
    run = await runner.start_run("budgeted", input={}, initiator_did="did:arc:x/1")

    await fail_node(tasks, f"wf-{run.run_id}-first-0", SALES_DID, "tool exploded")
    record = await runner.advance(run.run_id)

    assert record.status == "failed"
    assert "first" in (record.resolution or "")


async def test_all_nodes_done_rolls_the_run_into_done(stores: Any, registry: Any) -> None:
    _, runs, tasks = stores
    runner = build(stores, registry, CHAIN)
    run = await runner.start_run("budgeted", input={}, initiator_did="did:arc:x/1")

    await complete_node(tasks, f"wf-{run.run_id}-first-0", SALES_DID, {"ok": True})
    await runner.advance(run.run_id)
    await complete_node(tasks, f"wf-{run.run_id}-second-0", OPS_DID, {"ok": True})
    record = await runner.advance(run.run_id)

    assert record.status == "done"
    assert record.path_taken[-1]["kind"] == "outcome"


async def test_a_stalled_run_is_escalated_rather_than_sitting_silent(
    stores: Any, registry: Any
) -> None:
    """Nothing in flight, no frontier, work unfinished — that is a stall.

    Driven by a store that accepts the write and persists nothing: the silent
    failure mode the requirement exists for.
    """
    flow_tasks, _, tasks = stores
    runner = build(stores, registry, CHAIN)
    run = await runner.start_run("budgeted", input={}, initiator_did="did:arc:x/1")

    async def swallow(tasks_: Any, *, idempotency_keys: Any) -> list[Any]:
        return []

    await complete_node(tasks, f"wf-{run.run_id}-first-0", SALES_DID, {"ok": True})
    flow_tasks.create_batch = swallow  # type: ignore[method-assign]
    record = await runner.advance(run.run_id)

    assert record.status == "failed"
    assert "stall" in (record.resolution or "")
