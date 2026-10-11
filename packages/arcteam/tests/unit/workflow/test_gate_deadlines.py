"""Gate and human_input wait deadlines (SPEC-088 T-1349, COMP-009).

REQ-618..621, REQ-630, D-846; SDD "Timeouts" flow. ``timeout_s`` on a gate or
human_input node is an absolute wait deadline stamped on the row at
materialization (``metadata.wait_deadline``). Each runner pass applies the
node's ``on_timeout`` once the deadline has passed:

* ``fail``     -> row ``failed``; output ``decision=rejected, timed_out=true,
                  decided_by=<runner DID>``; the run follows ``on_failure``.
* ``continue`` -> row ``done``; ``approved`` (gate) / ``submitted``
                  (human_input), ``payload={}``, ``timed_out=true``.
* ``escalate`` -> first expiry adds the signed ``escalate_to`` as
                  ``metadata.escalated_to``, sets a new deadline, pushes a card
                  and a ``gate.escalated`` work event; a second expiry fails.

Every timeout write is the same conditional ``update_if`` a human decision
uses, so a race leaves exactly one decision.

The pure decision is ``GateDeadlines.due(row, now) -> TimeoutAction | None``,
callable on the class (it needs no state); ``TimeoutAction.kind`` names the
action. Everything else is asserted through the runner's public surface.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from arcstore.work_events import WorkEventLog

from arcteam.workflow.stores import WorkflowTaskStore

from ._human_steps import (
    ALICE,
    CAROL,
    PLANNER,
    T0,
    TIMEOUT_S,
    Clock,
    Harness,
    actions,
    approval_flow,
    input_flow,
)
from .conftest import RUNNER_DID, task_id


def gate_deadlines() -> Any:
    """The module under test, imported per test so each one fails on its own."""
    from arcteam.workflow.gate_deadlines import GateDeadlines

    return GateDeadlines


def harness(backend: Any, tmp_path: Path, definition: Any, **kwargs: Any) -> Harness:
    return Harness(backend=backend, definition=definition, root=tmp_path, clock=Clock(), **kwargs)


def failing_gate() -> Any:
    return approval_flow(output_schema="schemas/open.json", timeout_s=TIMEOUT_S)


def continuing_gate() -> Any:
    """A continue gate whose downstream step reads the decision, not a payload field.

    REQ-620 resolves with ``payload={}``, so a downstream ref to a payload field
    is correctly unresolvable (D-844); reading ``decision`` proves the run
    continues on the envelope.
    """
    return approval_flow(
        output_schema="schemas/open.json",
        timeout_s=TIMEOUT_S,
        on_timeout="continue",
        order_args={"decision": "$nodes.approve.output.decision"},
    )


def escalating_gate() -> Any:
    return approval_flow(
        output_schema="schemas/open.json",
        timeout_s=TIMEOUT_S,
        on_timeout="escalate",
        escalate_to=CAROL,
    )


# -- stamping --------------------------------------------------------------------


async def test_materialization_stamps_an_absolute_deadline(backend: Any, tmp_path: Path) -> None:
    h = harness(backend, tmp_path, escalating_gate())
    run_id = await h.reach("approve")

    gate = await h.row(run_id, "approve")

    deadline = gate.metadata.get("wait_deadline")
    assert deadline is not None, "no wait_deadline stamped on the gate row"
    assert datetime.fromisoformat(deadline) == T0 + timedelta(seconds=TIMEOUT_S)
    assert gate.metadata.get("on_timeout") == "escalate"
    assert gate.metadata.get("escalate_to") == CAROL


# -- the pure decision -----------------------------------------------------------


async def test_due_is_none_before_the_deadline_and_fail_after(
    backend: Any, tmp_path: Path
) -> None:
    h = harness(backend, tmp_path, failing_gate())
    run_id = await h.reach("approve")
    gate = await h.row(run_id, "approve")
    deadlines = gate_deadlines()

    assert deadlines.due(gate, T0 + timedelta(seconds=TIMEOUT_S - 1)) is None
    action = deadlines.due(gate, T0 + timedelta(seconds=TIMEOUT_S + 1))
    assert action is not None
    assert action.kind == "fail"


async def test_due_escalates_once_then_fails(backend: Any, tmp_path: Path) -> None:
    h = harness(backend, tmp_path, escalating_gate())
    run_id = await h.reach("approve")
    gate = await h.row(run_id, "approve")
    deadlines = gate_deadlines()
    late = T0 + timedelta(seconds=TIMEOUT_S + 1)

    first = deadlines.due(gate, late)
    assert first is not None and first.kind == "escalate"

    escalated = gate.model_copy(
        update={
            "metadata": {
                **gate.metadata,
                "escalated_to": CAROL,
                "wait_deadline": (late + timedelta(seconds=TIMEOUT_S)).isoformat(),
            }
        }
    )
    assert deadlines.due(escalated, late) is None
    second = deadlines.due(escalated, late + timedelta(seconds=TIMEOUT_S + 1))
    assert second is not None and second.kind == "fail"


async def test_due_ignores_a_row_that_is_no_longer_waiting(backend: Any, tmp_path: Path) -> None:
    h = harness(backend, tmp_path, failing_gate())
    run_id = await h.reach("approve")
    gate = await h.row(run_id, "approve")
    settled = gate.model_copy(update={"status": "done"})

    assert gate_deadlines().due(settled, T0 + timedelta(days=1)) is None


async def test_due_ignores_a_gate_with_no_deadline(backend: Any, tmp_path: Path) -> None:
    h = harness(backend, tmp_path, approval_flow(output_schema="schemas/open.json"))
    run_id = await h.reach("approve")
    gate = await h.row(run_id, "approve")

    assert gate.metadata.get("wait_deadline") is None
    assert gate_deadlines().due(gate, T0 + timedelta(days=365)) is None


# -- fail --------------------------------------------------------------------------


async def test_nothing_happens_before_the_deadline(backend: Any, tmp_path: Path) -> None:
    """Narrowness: a sweep that fires early is as wrong as one that never fires."""
    h = harness(backend, tmp_path, failing_gate())
    run_id = await h.reach("approve")
    # Proves the module exists, so this test cannot pass while it is absent.
    gate_deadlines()

    h.clock.advance(TIMEOUT_S - 1)
    await h.runner.advance(run_id)

    assert (await h.row(run_id, "approve")).status == "review"
    assert await h.run_status(run_id) == "waiting_gate"


async def test_fail_timeout_rejects_the_gate_and_fails_the_run(
    backend: Any, tmp_path: Path
) -> None:
    h = harness(backend, tmp_path, failing_gate())
    run_id = await h.reach("approve")

    h.clock.advance(TIMEOUT_S + 1)
    await h.runner.advance(run_id)

    gate = await h.row(run_id, "approve")
    assert gate.status == "failed"
    envelope = gate.output or {}
    assert envelope.get("decision") == "rejected"
    assert envelope.get("timed_out") is True
    assert envelope.get("decided_by") == RUNNER_DID
    assert await h.run_status(run_id) == "failed"
    [timed_out] = actions(h.sink, "workflow.gate.timed_out")
    assert timed_out.actor_did == RUNNER_DID


# -- continue ------------------------------------------------------------------------


async def test_continue_timeout_approves_a_gate_with_an_empty_payload(
    backend: Any, tmp_path: Path
) -> None:
    h = harness(backend, tmp_path, continuing_gate())
    run_id = await h.reach("approve")

    h.clock.advance(TIMEOUT_S + 1)
    await h.runner.advance(run_id)

    gate = await h.row(run_id, "approve")
    assert gate.status == "done"
    envelope = gate.output or {}
    assert envelope.get("decision") == "approved"
    assert envelope.get("payload") == {}
    assert envelope.get("timed_out") is True
    assert envelope.get("decided_by") == RUNNER_DID
    assert await h.run_status(run_id) != "failed"
    order = await h.row(run_id, "order")
    assert order.metadata["args"] == {"decision": "approved"}


async def test_continue_timeout_submits_a_human_input_with_an_empty_payload(
    backend: Any, tmp_path: Path
) -> None:
    h = harness(
        backend,
        tmp_path,
        input_flow(output_schema="schemas/open.json", timeout_s=TIMEOUT_S, on_timeout="continue"),
    )
    run_id = await h.reach("ask_due")

    h.clock.advance(TIMEOUT_S + 1)
    await h.runner.advance(run_id)

    row = await h.row(run_id, "ask_due")
    assert row.status == "done"
    envelope = row.output or {}
    assert envelope.get("decision") == "submitted"
    assert envelope.get("payload") == {}
    assert envelope.get("timed_out") is True
    assert task_id(run_id, "confirm", 0) in await h.rows(run_id)


# -- escalate -------------------------------------------------------------------------


async def test_first_expiry_escalates_once(backend: Any, tmp_path: Path) -> None:
    log = WorkEventLog(backend)
    h = harness(backend, tmp_path, escalating_gate(), runner_kwargs={"work_events": log})
    run_id = await h.reach("approve")
    cards_before = list(h.notifier.notices)

    h.clock.advance(TIMEOUT_S + 1)
    escalated_at = h.clock.now
    await h.runner.advance(run_id)
    # A second pass at the same instant must not escalate again.
    await h.runner.advance(run_id)

    gate = await h.row(run_id, "approve")
    assert gate.status == "review", "escalation settled the gate"
    assert gate.metadata.get("escalated_to") == CAROL
    assert datetime.fromisoformat(gate.metadata["wait_deadline"]) == escalated_at + timedelta(
        seconds=TIMEOUT_S
    )
    assert await h.run_status(run_id) == "waiting_gate"
    new_cards = h.notifier.notices[len(cards_before) :]
    assert len(new_cards) == 1, new_cards
    assert new_cards[0][1] not in {key for _, key in cards_before}
    assert len(actions(h.sink, "workflow.gate.escalated")) == 1
    events = [e for e in await log.read(0, limit=100) if e.kind == "gate.escalated"]
    assert len(events) == 1
    assert events[0].run_id == run_id
    assert events[0].node_id == "approve"


async def test_escalated_approver_may_decide_after_escalation(
    backend: Any, tmp_path: Path
) -> None:
    h = harness(backend, tmp_path, escalating_gate())
    run_id = await h.reach("approve")
    h.clock.advance(TIMEOUT_S + 1)
    await h.runner.advance(run_id)

    result = await h.plane.resolve_gate(
        task_id(run_id, "approve", 0),
        decision="approve",
        payload={},
        decider_did=CAROL,
        on_behalf_of=None,
        actor_roles=frozenset(),
    )

    assert result.ok, result.errors
    assert ((await h.row(run_id, "approve")).output or {}).get("decided_by") == CAROL


async def test_second_expiry_after_escalation_fails(backend: Any, tmp_path: Path) -> None:
    h = harness(backend, tmp_path, escalating_gate())
    run_id = await h.reach("approve")
    h.clock.advance(TIMEOUT_S + 1)
    await h.runner.advance(run_id)
    assert (await h.row(run_id, "approve")).metadata.get("escalated_to") == CAROL

    h.clock.advance(TIMEOUT_S + 1)
    await h.runner.advance(run_id)

    gate = await h.row(run_id, "approve")
    assert gate.status == "failed"
    envelope = gate.output or {}
    assert envelope.get("decision") == "rejected"
    assert envelope.get("timed_out") is True
    assert envelope.get("decided_by") == RUNNER_DID
    assert await h.run_status(run_id) == "failed"
    assert len(actions(h.sink, "workflow.gate.escalated")) == 1


# -- race ---------------------------------------------------------------------------


class RacingTasks(WorkflowTaskStore):
    """Holds the first two settling writes to one row until BOTH have arrived.

    Both the human decision and the timeout read the row as ``review`` before
    either writes, which is the interleaving a real race produces. A path that
    settles the row with anything but this conditional write never reaches the
    barrier, and the test fails on the timeout instead of passing by luck.
    """

    def __init__(self, backend: Any) -> None:
        super().__init__(backend, actor_did=RUNNER_DID)
        self.target: str | None = None
        self.barrier = asyncio.Barrier(2)
        self.contenders: list[str] = []

    async def update_if(
        self,
        task_id: str,
        patch: dict[str, Any],
        *,
        where: dict[str, Any],
        actor_did: str,
        fence: Any | None = None,
    ) -> Any:
        if task_id == self.target and "status" in patch and len(self.contenders) < 2:
            self.contenders.append(actor_did)
            try:
                await asyncio.wait_for(self.barrier.wait(), timeout=2.0)
            except TimeoutError as exc:
                raise AssertionError(
                    f"only {self.contenders} reached the conditional write; the other "
                    "side settled the row some other way"
                ) from exc
        return await super().update_if(
            task_id, patch, where=where, actor_did=actor_did, fence=fence
        )


async def test_a_human_decision_racing_the_timeout_leaves_exactly_one_decision(
    backend: Any, tmp_path: Path
) -> None:
    racing = RacingTasks(backend)
    h = harness(backend, tmp_path, failing_gate(), tasks_override=racing)
    run_id = await h.reach("approve")
    racing.target = task_id(run_id, "approve", 0)
    h.clock.advance(TIMEOUT_S + 1)

    human, sweep = await asyncio.gather(
        h.plane.resolve_gate(
            racing.target,
            decision="approve",
            payload={"any": "thing"},
            decider_did=ALICE,
            on_behalf_of=None,
            actor_roles=PLANNER,
        ),
        h.runner.advance(run_id),
        return_exceptions=True,
    )
    assert not isinstance(human, BaseException), human
    assert not isinstance(sweep, BaseException), sweep
    await h.runner.advance(run_id)

    assert sorted(racing.contenders) == sorted([ALICE, RUNNER_DID])
    gate = await h.row(run_id, "approve")
    envelope = gate.output or {}
    timed_out = actions(h.sink, "workflow.gate.timed_out")
    if gate.status == "done":
        assert human.ok
        assert envelope.get("decided_by") == ALICE
        assert envelope.get("timed_out") is False
        assert timed_out == []
    else:
        assert gate.status == "failed"
        assert not human.ok, "the losing human decision was reported as applied"
        assert envelope.get("decided_by") == RUNNER_DID
        assert envelope.get("timed_out") is True
        assert len(timed_out) == 1
    run = await h.runs.get(run_id)
    assert run is not None
    gate_entries = [
        e for e in run.path_taken if e.get("kind") == "gate" and e.get("node_id") == "approve"
    ]
    assert len(gate_entries) == 1, gate_entries


# -- restart ------------------------------------------------------------------------


async def test_a_runner_restarted_past_the_deadline_applies_it_on_its_first_pass(
    backend: Any, tmp_path: Path
) -> None:
    clock = Clock()
    before = Harness(backend=backend, definition=failing_gate(), root=tmp_path, clock=clock)
    run_id = await before.reach("approve")

    clock.advance(TIMEOUT_S * 10)
    after = Harness(backend=backend, definition=failing_gate(), root=tmp_path, clock=clock)
    await after.runner.resume()
    await after.runner.tick()

    gate = await after.row(run_id, "approve")
    assert gate.status == "failed"
    assert (gate.output or {}).get("timed_out") is True
    assert await after.run_status(run_id) == "failed"
    assert len(actions(after.sink, "workflow.gate.timed_out")) == 1
