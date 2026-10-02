"""Gate approvers — only the operator, a listed DID, or a listed role decides (alpha-2 #67).

Before this a gate had no approver: any paired chat user could ``/gate`` it.
Driven through the real control plane, the real runner and real task rows; the
only fakes are the stores and the registry, which here carries each member's
registered roles — the ONE place a decider's roles may come from.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from arcteam.workflow import GateNode, GateNotAuthorizedError
from arcteam.workflow.control_plane import OPERATOR_ROLE, WorkflowControlPlane
from arcteam.workflow.narrator import RunNarrator
from arcteam.workflow.runner import WorkflowRunError, WorkflowRunner

from .conftest import (
    RUNNER_DID,
    SALES_DID,
    Bundle,
    Definition,
    Node,
    RecordingNotifier,
    RecordingSender,
    RecordingSink,
    Registry,
    complete_node,
    evaluate,
    resolve_args,
    task_id,
)

OPERATOR = "did:arc:ui:operator"
PAIRED_USER = "did:arc:telegram:4242"
REVIEWER_USER = "did:arc:telegram:7777"
LISTED_USER = "did:arc:telegram:1001"
OPERATOR_ROLES = frozenset({OPERATOR_ROLE})


class Definitions:
    def __init__(self, bundle: Bundle) -> None:
        self._bundle = bundle

    def load(self, workflow_id: str) -> Bundle:
        return self._bundle

    def load_for_run(self, workflow_id: str) -> Bundle:
        return self._bundle

    def load_for_dispatch(self, workflow_id: str) -> Bundle:
        return self._bundle


def gated(approvers: tuple[str, ...] = (), channel: str | None = None) -> Definition:
    """draft -> review gate (with ``approvers``) -> publish."""
    return Definition(
        id="gated",
        owner="@sales",
        channel=channel,
        nodes=(
            Node(id="draft", kind="agent", agent="@sales"),
            Node(
                id="review",
                kind="gate",
                needs=("draft",),
                gate="human:approve",
                approvers=approvers,
            ),
            Node(id="publish", kind="agent", agent="@sales", needs=("review",)),
        ),
    )


def team(roles: dict[str, tuple[str, ...]] | None = None) -> Registry:
    return Registry(
        {"sales": SALES_DID},
        roles={SALES_DID: ("sales",), PAIRED_USER: ("sales",), REVIEWER_USER: ("reviewer",)}
        if roles is None
        else roles,
    )


def build(
    stores: Any,
    definition: Definition,
    registry: Registry,
    sink: RecordingSink,
    *,
    notifier: RecordingNotifier | None = None,
    narrator: RunNarrator | None = None,
) -> tuple[WorkflowRunner, WorkflowControlPlane]:
    flow_tasks, runs, _ = stores
    runner = WorkflowRunner(
        tasks=flow_tasks,
        runs=runs,
        definitions=Definitions(Bundle(definition)),
        owners=registry,
        roles=registry,
        runner_did=RUNNER_DID,
        tier="personal",
        evaluate=evaluate,
        resolve_args=resolve_args,
        audit_sink=sink,
        operator_notifier=notifier,
        narrator=narrator,
    )
    plane = WorkflowControlPlane(
        definitions=Definitions(Bundle(definition)),
        parse=lambda document: document,
        validate=lambda definition, **_: (),
        runner=runner,
        runs=runs,
        tier="personal",
        audit_sink=sink,
    )
    return runner, plane


async def at_the_gate(
    stores: Any, definition: Definition, registry: Registry, sink: RecordingSink, **kwargs: Any
) -> tuple[WorkflowRunner, WorkflowControlPlane, str]:
    _, _, tasks = stores
    runner, plane = build(stores, definition, registry, sink, **kwargs)
    run = await runner.start_run("gated", input={}, initiator="operator", initiator_did=OPERATOR)
    await complete_node(tasks, task_id(run.run_id, "draft", 0), SALES_DID, {"draft": "v1"})
    await runner.advance(run.run_id)
    return runner, plane, run.run_id


async def gate_row(stores: Any, run_id: str) -> Any:
    flow_tasks, _, _ = stores
    rows = {r.id: r for r in await flow_tasks.query_by_flow_run(run_id)}
    return rows[task_id(run_id, "review", 0)]


def denials(sink: RecordingSink) -> list[Any]:
    return [e for e in sink.events if e.action == "workflow.gate.denied"]


# -- who may decide ----------------------------------------------------------


async def test_gate_resolve_refused_for_unlisted_paired_user(stores: Any) -> None:
    """A paired chat user whose registry roles are not listed is denied + audited."""
    sink = RecordingSink()
    registry = team()
    _, plane, run_id = await at_the_gate(stores, gated(("role:reviewer",)), registry, sink)
    gate = task_id(run_id, "review", 0)

    with pytest.raises(GateNotAuthorizedError):
        await plane.resolve_gate(
            gate,
            decision="approve",
            actor_did=PAIRED_USER,
            actor_roles=await registry.roles_of(PAIRED_USER),
        )

    assert (await gate_row(stores, run_id)).status == "review", "a denied decision wrote the row"
    [denied] = denials(sink)
    assert denied.actor_did == PAIRED_USER
    assert denied.extra["task_id"] == gate
    assert denied.extra["actor_did"] == PAIRED_USER


async def test_gate_with_no_approvers_refuses_everyone_but_the_operator(stores: Any) -> None:
    sink = RecordingSink()
    _, plane, run_id = await at_the_gate(stores, gated(), team(), sink)

    with pytest.raises(GateNotAuthorizedError):
        await plane.resolve_gate(
            task_id(run_id, "review", 0),
            decision="approve",
            actor_did=REVIEWER_USER,
            actor_roles=frozenset({"reviewer"}),
        )
    assert len(denials(sink)) == 1


async def test_gate_resolve_allowed_for_operator_by_default(stores: Any) -> None:
    sink = RecordingSink()
    runner, plane, run_id = await at_the_gate(stores, gated(), team(), sink)

    result = await plane.resolve_gate(
        task_id(run_id, "review", 0),
        decision="approve",
        actor_did=OPERATOR,
        actor_roles=OPERATOR_ROLES,
    )

    assert result.ok
    assert (await gate_row(stores, run_id)).status == "done"
    resolved = [
        e for e in sink.events if e.action == "workflow.gate.resolved" and e.actor_did == OPERATOR
    ]
    assert [e.extra["decision"] for e in resolved] == ["approved"]


async def test_operator_decides_even_a_gate_that_lists_others(stores: Any) -> None:
    sink = RecordingSink()
    _, plane, run_id = await at_the_gate(stores, gated((LISTED_USER,)), team(), sink)

    result = await plane.resolve_gate(
        task_id(run_id, "review", 0),
        decision="fail_run",
        actor_did=OPERATOR,
        actor_roles=OPERATOR_ROLES,
    )

    assert result.ok


async def test_gate_resolve_allowed_for_declared_role(stores: Any) -> None:
    sink = RecordingSink()
    registry = team()
    _, plane, run_id = await at_the_gate(stores, gated(("role:reviewer",)), registry, sink)

    result = await plane.resolve_gate(
        task_id(run_id, "review", 0),
        decision="approve",
        actor_did=REVIEWER_USER,
        actor_roles=await registry.roles_of(REVIEWER_USER),
    )

    assert result.ok
    assert (await gate_row(stores, run_id)).metadata["gate_actor_did"] == REVIEWER_USER


async def test_gate_resolve_allowed_for_listed_did(stores: Any) -> None:
    sink = RecordingSink()
    _, plane, run_id = await at_the_gate(stores, gated((LISTED_USER,)), team(), sink)

    result = await plane.resolve_gate(
        task_id(run_id, "review", 0),
        decision="approve",
        actor_did=LISTED_USER,
        actor_roles=frozenset(),
    )

    assert result.ok


async def test_a_listed_did_never_matches_by_prefix(stores: Any) -> None:
    sink = RecordingSink()
    _, plane, run_id = await at_the_gate(stores, gated((LISTED_USER,)), team(), sink)

    with pytest.raises(GateNotAuthorizedError):
        await plane.resolve_gate(
            task_id(run_id, "review", 0),
            decision="approve",
            actor_did=LISTED_USER + "0",
            actor_roles=frozenset(),
        )


async def test_approvers_come_from_the_signed_definition_not_the_row(stores: Any) -> None:
    """Writing approvers onto the mutable row grants nothing."""
    flow_tasks, _, _ = stores
    sink = RecordingSink()
    _, plane, run_id = await at_the_gate(stores, gated(), team(), sink)
    gate = task_id(run_id, "review", 0)
    row = await gate_row(stores, run_id)
    await flow_tasks.update_if(
        gate,
        {"metadata": {**dict(row.metadata), "approvers": [PAIRED_USER]}},
        where={"status": "review"},
        actor_did=PAIRED_USER,
    )

    with pytest.raises(GateNotAuthorizedError):
        await plane.resolve_gate(
            gate, decision="approve", actor_did=PAIRED_USER, actor_roles=frozenset()
        )


async def test_a_replayed_approve_is_refused(stores: Any) -> None:
    sink = RecordingSink()
    _, plane, run_id = await at_the_gate(stores, gated(), team(), sink)
    gate = task_id(run_id, "review", 0)
    first = await plane.resolve_gate(
        gate, decision="approve", actor_did=OPERATOR, actor_roles=OPERATOR_ROLES
    )

    replay = await plane.resolve_gate(
        gate, decision="approve", actor_did=OPERATOR, actor_roles=OPERATOR_ROLES
    )

    assert first.ok
    assert not replay.ok
    assert replay.errors[0].error == "gate was already resolved"
    outcomes = [
        e.outcome
        for e in sink.events
        if e.action == "workflow.gate.resolved" and e.actor_did == OPERATOR
    ]
    assert outcomes == ["approved", "replayed"]


# -- run start ---------------------------------------------------------------


async def test_unknown_role_in_approvers_refuses_run_start(stores: Any) -> None:
    sink = RecordingSink()
    runner, _ = build(stores, gated(("role:ghost",)), team(), sink)

    with pytest.raises(WorkflowRunError, match="role:ghost"):
        await runner.start_run("gated", input={}, initiator="operator", initiator_did=OPERATOR)

    refused = [e for e in sink.events if e.action == "workflow.run.refused"]
    assert refused and refused[0].outcome == "unknown_gate_role"
    _, runs, _ = stores
    assert await runs.count_runs_for_workflow("gated") == 0


async def test_a_role_gate_with_no_roster_refuses_run_start(stores: Any) -> None:
    sink = RecordingSink()
    runner, _ = build(stores, gated(("role:reviewer",)), team(roles={}), sink)

    with pytest.raises(WorkflowRunError):
        await runner.start_run("gated", input={}, initiator="operator", initiator_did=OPERATOR)


# -- the approval card -------------------------------------------------------


async def test_gate_materialization_pushes_approval_card_once(stores: Any) -> None:
    sink = RecordingSink()
    notifier = RecordingNotifier()
    sender = RecordingSender()
    narrator = RunNarrator(sender, sender_did=RUNNER_DID)
    runner, _, run_id = await at_the_gate(
        stores,
        gated(channel="channel://ops"),
        team(),
        sink,
        notifier=notifier,
        narrator=narrator,
    )
    await runner.advance(run_id)
    await runner.advance(run_id)

    gate = task_id(run_id, "review", 0)
    cards = [n for n in notifier.notices if n[1] == f"workflow-gate:{gate}"]
    assert len(cards) == 1, notifier.notices
    text = cards[0][0]
    for verb in ("approve", "reject", "revise"):
        assert f"/gate {gate} {verb}" in text
    posted = [m for m in sender.sent if m.meta.get("event") == "gate.waiting"]
    assert len(posted) == 1
    assert f"/gate {gate} approve" in posted[0].body
    pushed = [e for e in sink.events if e.action == "workflow.gate.card_pushed"]
    assert [e.outcome for e in pushed] == ["delivered"]


# -- definition syntax -------------------------------------------------------


@pytest.mark.parametrize(
    "approver", ["reviewer", "role:", "role:bad name", "@sales", "did:", "ROLE:reviewer"]
)
def test_unknown_approver_syntax_is_rejected(approver: str) -> None:
    with pytest.raises(ValidationError):
        GateNode(id="review", kind="gate", gate="human:approve", approvers=(approver,))


def test_dids_and_roles_are_accepted() -> None:
    node = GateNode(
        id="review",
        kind="gate",
        gate="human:approve",
        approvers=("did:arc:telegram:12345", "role:reviewer"),
    )
    assert node.approvers == ("did:arc:telegram:12345", "role:reviewer")
