"""T-861 — run budget (reserve-then-settle), stall detection, terminal roll-up.

REQ-228, REQ-236.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from arcteam.workflow.runner_budget import RunBudget

from .conftest import (
    OPS_DID,
    SALES_DID,
    Budget,
    Definition,
    Node,
    complete_node,
    fail_node,
    task_id,
)
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
# The accounting itself (ported from arcagent's since-removed planning module)
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

    await complete_node(
        tasks, task_id(run.run_id, "first", 0), SALES_DID, {"ok": True}, tokens=140
    )
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

    await complete_node(tasks, task_id(run.run_id, "first", 0), SALES_DID, {"ok": True}, tokens=20)
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

    await fail_node(tasks, task_id(run.run_id, "first", 0), SALES_DID, "tool exploded")
    record = await runner.advance(run.run_id)

    assert record.status == "failed"
    assert "first" in (record.resolution or "")


async def test_all_nodes_done_rolls_the_run_into_done(stores: Any, registry: Any) -> None:
    _, runs, tasks = stores
    runner = build(stores, registry, CHAIN)
    run = await runner.start_run("budgeted", input={}, initiator_did="did:arc:x/1")

    await complete_node(tasks, task_id(run.run_id, "first", 0), SALES_DID, {"ok": True})
    await runner.advance(run.run_id)
    await complete_node(tasks, task_id(run.run_id, "second", 0), OPS_DID, {"ok": True})
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

    async def swallow(tasks_: Any, *, actor_did: str) -> list[Any]:
        return []

    await complete_node(tasks, task_id(run.run_id, "first", 0), SALES_DID, {"ok": True})
    flow_tasks.create_batch = swallow  # type: ignore[method-assign]
    record = await runner.advance(run.run_id)

    assert record.status == "failed"
    assert "stall" in (record.resolution or "")


class _TwoWorkflows:
    """A definition store serving two workflows, one of which is poisoned."""

    def __init__(self, bundles: dict, poisoned: str | None = None) -> None:
        self._bundles = bundles
        self.poisoned = poisoned

    def load(self, workflow_id: str):
        return self._bundles[workflow_id]

    def load_for_run(self, workflow_id: str):
        return self._bundles[workflow_id]

    def load_for_dispatch(self, workflow_id: str):
        if workflow_id == self.poisoned:
            raise RuntimeError("this run's bundle is unreadable")
        return self._bundles[workflow_id]


async def test_one_poisoned_run_does_not_stop_the_tick_for_the_others(
    stores: Any, registry: Any
) -> None:
    """A single bad run must not stall every other run in the fleet.

    This repo has shipped this failure before: one unreadable row in a
    scheduler's store silently killed every schedule that agent had. The tick
    isolates per run for exactly that reason, and nothing exercised it — the
    happy path never has a poisoned run, so the guard was invisible.
    """
    from .conftest import Bundle

    alpha = Definition(id="alpha", nodes=(Node(id="a", kind="agent", agent="@sales"),))
    beta = Definition(id="beta", nodes=(Node(id="b", kind="agent", agent="@ops"),))
    definitions = _TwoWorkflows({"alpha": Bundle(alpha), "beta": Bundle(beta)})
    flow_tasks, _, _ = stores
    runner = build(stores, registry, alpha, definitions=definitions)

    await runner.start_run("alpha", input={}, initiator_did="did:arc:x/1", run_id="run-alpha")
    await runner.start_run("beta", input={}, initiator_did="did:arc:x/1", run_id="run-beta")

    # Alpha's definition becomes unreadable between ticks.
    definitions.poisoned = "alpha"
    advanced = await runner.tick()

    assert advanced == 2, "the tick must visit every active run, not stop at the first"
    beta_rows = await flow_tasks.query_by_flow_run("run-beta")
    assert [r.metadata["node_id"] for r in beta_rows] == ["b"], (
        "beta was starved by alpha's failure"
    )


async def test_an_unreadable_start_time_stops_the_run_rather_than_unbounding_it(
    stores: Any, registry: Any, backend: Any
) -> None:
    """A budget guard that cannot evaluate must refuse, not wave the run through.

    The old code caught the parse error and returned "not exceeded", which is
    invisible on every well-formed run and silently removes the ONLY bound on
    how long a run may burn (REQ-236). The question a swallow always raises is
    what the swallowed case should have produced — here, a stop.
    """
    _, runs, _ = stores
    runner = build(stores, registry, CHAIN)
    run = await runner.start_run("budgeted", input={}, initiator_did="did:arc:x/1")

    await backend.mutable_merge(
        "runs", run.run_id, {"started_at": "not-a-timestamp"}, actor_did="did:arc:x/1"
    )
    record = await runner.advance(run.run_id)

    assert record.status == "failed"
    assert "unreadable start time" in (record.resolution or "")


async def test_a_missing_start_time_stops_the_run_too(
    stores: Any, registry: Any, backend: Any
) -> None:
    """Same guard, the other unevaluable case."""
    _, runs, _ = stores
    runner = build(stores, registry, CHAIN)
    run = await runner.start_run("budgeted", input={}, initiator_did="did:arc:x/1")

    await backend.mutable_merge("runs", run.run_id, {"started_at": None}, actor_did="did:arc:x/1")
    record = await runner.advance(run.run_id)

    assert record.status == "failed"
    assert "cannot be bounded" in (record.resolution or "")
