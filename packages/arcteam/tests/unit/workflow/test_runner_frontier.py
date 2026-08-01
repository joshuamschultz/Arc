"""T-842 — lazy materialization, routers, loops, path taken (REQ-220, REQ-229).

Every test drives the runner's real progression path over real task rows.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from arcteam.workflow.runner import WorkflowRunner

from .conftest import (
    OPS_DID,
    REVIEWER_DID,
    RUNNER_DID,
    SALES_DID,
    Budget,
    Bundle,
    Definition,
    Node,
    RecordingSink,
    Route,
    complete_node,
    evaluate,
    path_kinds,
    resolve_args,
)


class FakeDefinitions:
    def __init__(self, bundle: Bundle) -> None:
        self._bundle = bundle
        self.load_for_run_calls = 0

    def load(self, workflow_id: str) -> Bundle:
        return self._bundle

    def load_for_run(self, workflow_id: str) -> Bundle:
        self.load_for_run_calls += 1
        return self._bundle


def onboarding() -> Definition:
    return Definition(
        id="customer-onboarding",
        version=4,
        owner="@sales",
        channel="channel://onboarding",
        budget=Budget(tokens=400_000, wall_clock_s=1800),
        nodes=(
            Node(id="collect", kind="agent", agent="@sales"),
            Node(
                id="verify",
                kind="tool",
                agent="@sales",
                needs=("collect",),
                tool="crm_lookup",
                args={"domain": "$nodes.collect.output.company_domain"},
            ),
            Node(
                id="risk_router",
                kind="router",
                mode="rules",
                needs=("verify",),
                routes=(
                    Route(to="provision", when="$nodes.verify.output.risk == 'low'"),
                    Route(to="manual_review", default=True),
                ),
            ),
            Node(id="manual_review", kind="gate", gate="human:approve", needs=("risk_router",)),
            Node(id="provision", kind="script", agent="@ops", needs=("risk_router",)),
            Node(
                id="qa",
                kind="agent",
                agent="@reviewer",
                needs=("provision", "manual_review"),
                join="any",
            ),
        ),
    )


def build(stores: Any, registry: Any, definition: Definition, **kwargs: Any) -> Any:
    flow_tasks, runs, _ = stores
    return WorkflowRunner(
        tasks=flow_tasks,
        runs=runs,
        definitions=kwargs.pop("definitions", FakeDefinitions(Bundle(definition))),
        owners=registry,
        runner_did=RUNNER_DID,
        tier=kwargs.pop("tier", "personal"),
        evaluate=evaluate,
        resolve_args=resolve_args,
        **kwargs,
    )


async def test_only_reachable_nodes_materialize(stores: Any, registry: Any) -> None:
    flow_tasks, _, _ = stores
    runner = build(stores, registry, onboarding())

    run = await runner.start_run(
        "customer-onboarding", input={}, initiator_did="did:arc:local:user/9999"
    )

    rows = await flow_tasks.query_by_flow_run(run.run_id)
    assert [r.metadata["node_id"] for r in rows] == ["collect"]
    assert rows[0].owner_did == SALES_DID
    assert rows[0].creator_did == RUNNER_DID


async def test_untaken_branch_never_becomes_a_task_row(stores: Any, registry: Any) -> None:
    flow_tasks, runs, tasks = stores
    runner = build(stores, registry, onboarding())
    run = await runner.start_run("customer-onboarding", input={}, initiator_did="did:arc:x/1")

    await complete_node(tasks, f"wf-{run.run_id}-collect-0", SALES_DID, {"company_domain": "a.io"})
    await runner.advance(run.run_id)
    await complete_node(tasks, f"wf-{run.run_id}-verify-0", SALES_DID, {"risk": "low"})
    await runner.advance(run.run_id)

    rows = await flow_tasks.query_by_flow_run(run.run_id)
    materialized = {r.metadata["node_id"] for r in rows}
    assert "provision" in materialized
    assert "manual_review" not in materialized, "an untaken branch must never be a task row"

    record = await runs.get(run.run_id)
    skips = [e for e in record.path_taken if e["kind"] == "skipped"]
    assert [e["node_id"] for e in skips] == ["manual_review"]


async def test_router_choice_is_recorded_in_the_path_taken(stores: Any, registry: Any) -> None:
    _, runs, tasks = stores
    runner = build(stores, registry, onboarding())
    run = await runner.start_run("customer-onboarding", input={}, initiator_did="did:arc:x/1")

    await complete_node(tasks, f"wf-{run.run_id}-collect-0", SALES_DID, {"company_domain": "a.io"})
    await runner.advance(run.run_id)
    await complete_node(tasks, f"wf-{run.run_id}-verify-0", SALES_DID, {"risk": "high"})
    await runner.advance(run.run_id)

    record = await runs.get(run.run_id)
    route = next(e for e in record.path_taken if e["kind"] == "route")
    assert route["node_id"] == "risk_router"
    assert route["chosen"] == "manual_review"
    assert route["skipped"] == ["provision"]
    assert "materialized" in path_kinds(record)


async def test_gate_node_materializes_as_a_review_task(stores: Any, registry: Any) -> None:
    flow_tasks, _, tasks = stores
    runner = build(stores, registry, onboarding())
    run = await runner.start_run("customer-onboarding", input={}, initiator_did="did:arc:x/1")

    await complete_node(tasks, f"wf-{run.run_id}-collect-0", SALES_DID, {"company_domain": "a.io"})
    await runner.advance(run.run_id)
    await complete_node(tasks, f"wf-{run.run_id}-verify-0", SALES_DID, {"risk": "high"})
    await runner.advance(run.run_id)

    rows = {r.metadata["node_id"]: r for r in await flow_tasks.query_by_flow_run(run.run_id)}
    assert rows["manual_review"].requires_review is True


async def test_tool_node_args_resolve_by_value_from_upstream_output(
    stores: Any, registry: Any
) -> None:
    flow_tasks, _, tasks = stores
    runner = build(stores, registry, onboarding())
    run = await runner.start_run("customer-onboarding", input={}, initiator_did="did:arc:x/1")

    await complete_node(
        tasks, f"wf-{run.run_id}-collect-0", SALES_DID, {"company_domain": "acme.example"}
    )
    await runner.advance(run.run_id)

    rows = {r.metadata["node_id"]: r for r in await flow_tasks.query_by_flow_run(run.run_id)}
    verify = rows["verify"]
    assert verify.metadata["tool"] == "crm_lookup"
    assert verify.metadata["args"] == {"domain": "acme.example"}


async def test_llm_router_records_only_a_declared_choice(stores: Any, registry: Any) -> None:
    """A model picks AMONG declared routes; it may never invent a destination."""
    definition = Definition(
        id="triage",
        channel="channel://triage",
        nodes=(
            Node(
                id="pick",
                kind="router",
                mode="llm",
                agent="@sales",
                routes=(Route(to="fast"), Route(to="slow", default=True)),
            ),
            Node(id="fast", kind="agent", agent="@ops", needs=("pick",)),
            Node(id="slow", kind="agent", agent="@ops", needs=("pick",)),
        ),
    )
    flow_tasks, runs, tasks = stores
    runner = build(stores, registry, definition)
    run = await runner.start_run("triage", input={}, initiator_did="did:arc:x/1")

    rows = await flow_tasks.query_by_flow_run(run.run_id)
    assert [r.metadata["node_id"] for r in rows] == ["pick"], "an llm router runs as a node"

    await complete_node(tasks, f"wf-{run.run_id}-pick-0", SALES_DID, {"route": "fast"})
    await runner.advance(run.run_id)

    materialized = {
        r.metadata["node_id"] for r in await flow_tasks.query_by_flow_run(run.run_id)
    }
    assert "fast" in materialized and "slow" not in materialized
    record = await runs.get(run.run_id)
    assert next(e for e in record.path_taken if e["kind"] == "route")["chosen"] == "fast"


async def test_llm_router_undeclared_choice_fails_the_run(stores: Any, registry: Any) -> None:
    definition = Definition(
        id="triage",
        nodes=(
            Node(
                id="pick",
                kind="router",
                mode="llm",
                agent="@sales",
                routes=(Route(to="fast"), Route(to="slow", default=True)),
            ),
            Node(id="fast", kind="agent", agent="@ops", needs=("pick",)),
            Node(id="slow", kind="agent", agent="@ops", needs=("pick",)),
        ),
    )
    _, runs, tasks = stores
    runner = build(stores, registry, definition)
    run = await runner.start_run("triage", input={}, initiator_did="did:arc:x/1")

    await complete_node(tasks, f"wf-{run.run_id}-pick-0", SALES_DID, {"route": "sideways"})
    await runner.advance(run.run_id)

    record = await runs.get(run.run_id)
    assert record.status == "failed"
    assert "undeclared route" in (record.resolution or "")


async def test_a_node_that_would_need_interpolation_fails_closed(
    stores: Any, registry: Any
) -> None:
    """Upstream output binds as a VALUE or not at all (LLM01, expression injection)."""
    definition = Definition(
        id="wired",
        nodes=(
            Node(id="collect", kind="agent", agent="@sales"),
            Node(
                id="verify",
                kind="tool",
                agent="@sales",
                needs=("collect",),
                tool="crm_lookup",
                args={"query": "lookup $nodes.collect.output.company_domain now"},
            ),
        ),
    )
    flow_tasks, runs, tasks = stores
    runner = build(stores, registry, definition)
    run = await runner.start_run("wired", input={}, initiator_did="did:arc:x/1")

    await complete_node(tasks, f"wf-{run.run_id}-collect-0", SALES_DID, {"company_domain": "a.io"})
    record = await runner.advance(run.run_id)

    assert record.status == "failed"
    assert "verify" in (record.resolution or "")
    materialized = {
        r.metadata["node_id"] for r in await flow_tasks.query_by_flow_run(run.run_id)
    }
    assert materialized == {"collect"}


async def test_a_router_whose_predicate_cannot_be_evaluated_fails_closed(
    stores: Any, registry: Any
) -> None:
    """A missing upstream field takes NO branch — a silent false would take one."""
    flow_tasks, runs, tasks = stores
    runner = build(stores, registry, onboarding())
    run = await runner.start_run("customer-onboarding", input={}, initiator_did="did:arc:x/1")

    await complete_node(tasks, f"wf-{run.run_id}-collect-0", SALES_DID, {"company_domain": "a.io"})
    await runner.advance(run.run_id)
    # The node completes without the `risk` field its router routes on.
    await complete_node(tasks, f"wf-{run.run_id}-verify-0", SALES_DID, {"unrelated": 1})
    record = await runner.advance(run.run_id)

    assert record.status == "failed"
    assert "risk_router" in (record.resolution or "")
    materialized = {
        r.metadata["node_id"] for r in await flow_tasks.query_by_flow_run(run.run_id)
    }
    assert materialized == {"collect", "verify"}, "no branch may be taken on a failed predicate"


async def test_loop_mints_iteration_stamped_rows_then_fails_on_exhaustion(
    stores: Any, registry: Any
) -> None:
    definition = Definition(
        id="qa-loop",
        nodes=(
            Node(id="draft", kind="agent", agent="@ops"),
            Node(id="check", kind="agent", agent="@reviewer", needs=("draft",)),
            Node(
                id="revise",
                kind="agent",
                agent="@ops",
                needs=("check",),
                when="$nodes.check.output.verdict == 'revise'",
                loop_back_to="draft",
                max_iterations=3,
            ),
        ),
    )
    flow_tasks, runs, tasks = stores
    runner = build(stores, registry, definition)
    run = await runner.start_run("qa-loop", input={}, initiator_did="did:arc:x/1")

    for iteration in range(3):
        await complete_node(tasks, f"wf-{run.run_id}-draft-{iteration}", OPS_DID, {"draft": "x"})
        await runner.advance(run.run_id)
        await complete_node(
            tasks, f"wf-{run.run_id}-check-{iteration}", REVIEWER_DID, {"verdict": "revise"}
        )
        await runner.advance(run.run_id)
        await complete_node(tasks, f"wf-{run.run_id}-revise-{iteration}", OPS_DID, {"done": True})
        await runner.advance(run.run_id)

    rows = await flow_tasks.query_by_flow_run(run.run_id)
    drafts = sorted(r.metadata["iteration"] for r in rows if r.metadata["node_id"] == "draft")
    assert drafts == [0, 1, 2], "one fresh iteration-stamped row per loop entry, bounded at 3"

    record = await runs.get(run.run_id)
    assert record.status == "failed"
    assert "max_iterations" in (record.resolution or "")
    assert [e for e in record.path_taken if e["kind"] == "loop"]


async def test_when_false_skips_the_node_and_the_run_completes(
    stores: Any, registry: Any
) -> None:
    definition = Definition(
        id="qa-loop",
        nodes=(
            Node(id="draft", kind="agent", agent="@ops"),
            Node(id="check", kind="agent", agent="@reviewer", needs=("draft",)),
            Node(
                id="revise",
                kind="agent",
                agent="@ops",
                needs=("check",),
                when="$nodes.check.output.verdict == 'revise'",
                loop_back_to="draft",
                max_iterations=3,
            ),
        ),
    )
    flow_tasks, runs, tasks = stores
    runner = build(stores, registry, definition)
    run = await runner.start_run("qa-loop", input={}, initiator_did="did:arc:x/1")

    await complete_node(tasks, f"wf-{run.run_id}-draft-0", OPS_DID, {"draft": "x"})
    await runner.advance(run.run_id)
    await complete_node(
        tasks, f"wf-{run.run_id}-check-0", REVIEWER_DID, {"verdict": "approve"}
    )
    record = await runner.advance(run.run_id)

    assert record.status == "done"
    assert not [r for r in await flow_tasks.query_by_flow_run(run.run_id) if r.metadata["node_id"] == "revise"]


async def test_queries_are_scoped_by_run_not_list_then_filter(
    stores: Any, registry: Any
) -> None:
    flow_tasks, _, tasks = stores
    runner = build(stores, registry, onboarding())
    run = await runner.start_run("customer-onboarding", input={}, initiator_did="did:arc:x/1")
    await complete_node(tasks, f"wf-{run.run_id}-collect-0", SALES_DID, {"company_domain": "a.io"})
    await runner.advance(run.run_id)

    assert flow_tasks.scoped_queries, "the tick must read through the run-scoped query"
    assert set(flow_tasks.scoped_queries) == {run.run_id}


async def test_tier_is_taken_at_construction_and_stamped_on_every_audit_event(
    stores: Any, registry: Any
) -> None:
    sink = RecordingSink()
    runner = build(stores, registry, onboarding(), tier="federal", audit_sink=sink)
    await runner.start_run("customer-onboarding", input={}, initiator_did="did:arc:x/1")

    assert sink.events, "starting a run is an audited operation"
    assert {e.tier for e in sink.events} == {"federal"}
    assert "workflow.run.started" in sink.actions()
    assert "tier" not in inspect.signature(runner.start_run).parameters
    assert "tier" not in inspect.signature(runner.advance).parameters


def test_runner_holds_no_model_and_makes_no_llm_call() -> None:
    """Progression is deterministic code: no model in, no loop package imported."""
    source = Path(inspect.getfile(WorkflowRunner)).read_text()
    for forbidden in ("arcllm", "arcrun", "arcagent"):
        assert forbidden not in source, f"the runner must not reach for {forbidden}"
    params = set(inspect.signature(WorkflowRunner.__init__).parameters)
    assert not {"model", "llm", "capabilities", "system_prompt", "strategy"} & params


async def test_unsigned_definition_is_refused_above_personal_tier(
    stores: Any, registry: Any
) -> None:
    flow_tasks, runs, _ = stores
    runner = WorkflowRunner(
        tasks=flow_tasks,
        runs=runs,
        definitions=FakeDefinitions(Bundle(onboarding(), status="draft")),
        owners=registry,
        runner_did=RUNNER_DID,
        tier="federal",
        evaluate=evaluate,
        resolve_args=resolve_args,
    )
    with pytest.raises(Exception) as excinfo:
        await runner.start_run("customer-onboarding", input={}, initiator_did="did:arc:x/1")
    assert "signed" in str(excinfo.value)
