"""Who started the run decides whether an unsigned workflow may run (S-wf-unsigned).

An agent can author a workflow but cannot bless it. If an agent (or a schedule an
agent set up) could also *run* its own unsigned draft, authoring would be
self-approval at personal tier. The only unsigned run is an operator-initiated
draft test at personal tier, and it is audited with a warning.
"""

from __future__ import annotations

from typing import Any

import pytest

from arcteam.workflow.errors import UnsignedWorkflowError
from arcteam.workflow.runner import UnsignedWorkflowRefusedError

from .conftest import Bundle, RecordingSink
from .test_runner_frontier import FakeDefinitions, build, onboarding

ACTOR = "did:arc:x/1"


def _unsigned() -> Bundle:
    return Bundle(onboarding(), status="draft", signer_did=None)


def _runner(stores: Any, registry: Any, tier: str, sink: RecordingSink) -> Any:
    return build(
        stores,
        registry,
        onboarding(),
        tier=tier,
        definitions=FakeDefinitions(_unsigned()),
        audit_sink=sink,
    )


@pytest.mark.parametrize("tier", ["personal", "enterprise", "federal"])
@pytest.mark.parametrize("initiator", ["agent", "scheduler"])
async def test_agent_and_scheduler_never_start_an_unsigned_workflow(
    stores: Any, registry: Any, tier: str, initiator: str
) -> None:
    sink = RecordingSink()
    runner = _runner(stores, registry, tier, sink)

    with pytest.raises(UnsignedWorkflowRefusedError):
        await runner.start_run(
            "customer-onboarding", input={}, initiator=initiator, initiator_did=ACTOR
        )

    denied = [e for e in sink.events if e.outcome == "denied"]
    assert [e.action for e in denied] == ["workflow.run.started"]
    assert denied[0].extra["initiator"] == initiator
    assert await stores[1].active_runs() == []


@pytest.mark.parametrize("tier", ["enterprise", "federal"])
async def test_operator_draft_run_is_refused_above_personal(
    stores: Any, registry: Any, tier: str
) -> None:
    sink = RecordingSink()
    runner = _runner(stores, registry, tier, sink)

    with pytest.raises(UnsignedWorkflowRefusedError):
        await runner.start_run(
            "customer-onboarding", input={}, initiator="operator", initiator_did=ACTOR
        )

    assert [e.outcome for e in sink.events if e.action == "workflow.run.started"] == ["denied"]


async def test_operator_draft_run_at_personal_is_allowed_and_audited_with_warning(
    stores: Any, registry: Any
) -> None:
    sink = RecordingSink()
    runner = _runner(stores, registry, "personal", sink)

    run = await runner.start_run(
        "customer-onboarding", input={}, initiator="operator", initiator_did=ACTOR
    )

    assert run.status == "running"
    warned = [e for e in sink.events if e.action == "workflow.run.unsigned_draft"]
    assert len(warned) == 1
    assert warned[0].outcome == "warning"
    assert warned[0].extra["initiator"] == "operator"
    started = [e for e in sink.events if e.action == "workflow.run.started"]
    assert started[0].extra["initiator"] == "operator"


async def test_store_level_unsigned_refusal_is_audited_as_denied(
    stores: Any, registry: Any
) -> None:
    class _StoreRefuses(FakeDefinitions):
        def load_for_run(self, workflow_id: str) -> Bundle:
            raise UnsignedWorkflowError(workflow_id)

    sink = RecordingSink()
    runner = build(
        stores,
        registry,
        onboarding(),
        tier="enterprise",
        definitions=_StoreRefuses(_unsigned()),
        audit_sink=sink,
    )

    with pytest.raises(UnsignedWorkflowRefusedError):
        await runner.start_run(
            "customer-onboarding", input={}, initiator="agent", initiator_did=ACTOR
        )

    assert [e.outcome for e in sink.events if e.action == "workflow.run.started"] == ["denied"]


async def test_a_signed_workflow_runs_for_every_initiator(stores: Any, registry: Any) -> None:
    for initiator in ("operator", "agent", "scheduler"):
        runner = build(stores, registry, onboarding(), tier="federal")
        run = await runner.start_run(
            "customer-onboarding", input={}, initiator=initiator, initiator_did=ACTOR
        )
        assert run.status == "running"
