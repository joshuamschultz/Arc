"""``control_plane.test_run`` — try a draft at any tier without ever trusting it (J3 F6, G8).

An operator needs to see a draft work before signing it, but signing is what
makes a definition trusted. So a test run is a distinct mode: allowed unsigned at
every tier, flagged on the run and on every node row it creates (so the agent
executing a row stubs the dangerous tools), capped in cost, never reachable
from a schedule, and audited as ``workflow.test_run``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from arcteam.workflow.control_plane import WorkflowControlPlane
from arcteam.workflow.errors import UnsignedWorkflowError
from arcteam.workflow.runner import (
    TEST_RUN_MAX_COST_USD,
    UnsignedWorkflowRefusedError,
    is_test_run,
)

from .conftest import (
    SALES_DID,
    Bundle,
    Definition,
    Node,
    RecordingSink,
    complete_node,
    task_id,
)
from .test_runner_frontier import FakeDefinitions, build

OPERATOR = "did:arc:local:user/0f0f0f0f"
WORKFLOW = "crm-sync"


def _definition() -> Definition:
    return Definition(
        id=WORKFLOW,
        owner="@sales",
        nodes=(
            Node(id="collect", kind="agent", agent="@sales"),
            Node(
                id="push",
                kind="tool",
                agent="@sales",
                needs=("collect",),
                tool="crm_update",
                args={"record": "$nodes.collect.output.record"},
            ),
        ),
    )


class _StrictDefinitions(FakeDefinitions):
    """The real store's trust gates: above personal, an unsigned bundle never dispatches."""

    def __init__(self, bundle: Bundle, *, tier: str) -> None:
        super().__init__(bundle)
        self._tier = tier

    def load(self, workflow_id: str) -> Any:
        self._require_known(workflow_id)
        return super().load(workflow_id)

    def load_for_run(self, workflow_id: str) -> Any:
        self._require_known(workflow_id)
        self._refuse_unsigned(workflow_id)
        return super().load_for_run(workflow_id)

    def load_for_dispatch(self, workflow_id: str) -> Any:
        self._require_known(workflow_id)
        self._refuse_unsigned(workflow_id)
        return super().load_for_dispatch(workflow_id)

    @staticmethod
    def _require_known(workflow_id: str) -> None:
        if workflow_id != WORKFLOW:
            raise KeyError(workflow_id)

    def _refuse_unsigned(self, workflow_id: str) -> None:
        if not self._bundle.is_verified and self._tier != "personal":
            raise UnsignedWorkflowError(workflow_id)


def _plane(stores: Any, registry: Any, tier: str) -> tuple[WorkflowControlPlane, RecordingSink]:
    definition = _definition()
    definitions = _StrictDefinitions(
        Bundle(definition, status="draft", signer_did=None), tier=tier
    )
    sink = RecordingSink()
    runner = build(
        stores, registry, definition, tier=tier, definitions=definitions, audit_sink=sink
    )

    def _parse(document: Mapping[str, Any]) -> Definition:
        return definition

    plane = WorkflowControlPlane(
        definitions=definitions,
        parse=_parse,
        validate=lambda *_a, **_k: (),
        runner=runner,
        runs=stores[1],
        tier=tier,  # type: ignore[arg-type]  # reason: test passes a plain str
        audit_sink=sink,
    )
    return plane, sink


@pytest.mark.parametrize("tier", ["personal", "enterprise", "federal"])
async def test_state_modifying_tools_are_stubbed_and_run_never_scheduled(
    stores: Any, registry: Any, tier: str
) -> None:
    flow_tasks, _, tasks = stores
    plane, sink = _plane(stores, registry, tier)

    # The live path still refuses an unsigned draft where the tier demands it.
    live = await plane.run(WORKFLOW, input={}, initiator="operator", actor_did=OPERATOR)
    assert live.ok == (tier == "personal")

    result = await plane.test_run(WORKFLOW, actor_did=OPERATOR)

    assert result.ok and result.run is not None
    run = result.run
    assert is_test_run(run.run_id)
    assert run.budget_cost_usd == TEST_RUN_MAX_COST_USD == 0.50
    assert "workflow.test_run" in sink.actions()

    # Every row of a test run is flagged, so the agent that executes it stubs
    # state-modifying tools. The tool node appears only after the first node
    # completes — dispatch of an unsigned draft must not be refused mid-run.
    await complete_node(tasks, task_id(run.run_id, "collect", 0), SALES_DID, {"record": "r1"})
    await plane.runner.advance(run.run_id)
    rows = {r.metadata["node_id"]: r for r in await flow_tasks.query_by_flow_run(run.run_id)}
    assert rows["collect"].metadata["mode"] == "test"
    assert rows["push"].metadata["mode"] == "test"
    assert rows["push"].metadata["tool"] == "crm_update"

    # Never scheduled: a schedule or an agent cannot start this draft, and the
    # reserved test namespace cannot be claimed by a live start.
    with pytest.raises(UnsignedWorkflowRefusedError):
        await plane.runner.start_run(
            WORKFLOW, input={}, initiator="scheduler", initiator_did=OPERATOR
        )
    spoof = await plane.run(
        WORKFLOW, input={}, initiator="operator", actor_did=OPERATOR, run_id="test-forged"
    )
    assert not spoof.ok


async def test_a_live_run_is_not_flagged_as_a_test(stores: Any, registry: Any) -> None:
    flow_tasks, _, _ = stores
    plane, _ = _plane(stores, registry, "personal")

    result = await plane.run(WORKFLOW, input={}, initiator="operator", actor_did=OPERATOR)

    assert result.run is not None and not is_test_run(result.run.run_id)
    rows = await flow_tasks.query_by_flow_run(result.run.run_id)
    assert all("mode" not in r.metadata or r.metadata["mode"] == "live" for r in rows)


async def test_a_refused_test_run_is_reported_not_raised(stores: Any, registry: Any) -> None:
    plane, sink = _plane(stores, registry, "enterprise")

    result = await plane.test_run("missing", actor_did=OPERATOR)

    assert not result.ok
    assert any(e.action == "workflow.test_run" and e.outcome != "started" for e in sink.events)
