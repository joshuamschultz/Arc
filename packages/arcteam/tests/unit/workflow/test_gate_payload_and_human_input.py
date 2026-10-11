"""Gate payload envelope, decider identity and human_input (SPEC-088 T-1347).

REQ-611..616, D-844, D-845; SDD COMP-007 / COMP-008 and the "Gate or input
resolution" flow. Driven through the real control plane, runner and task rows.

Contract these tests pin (SDD COMP-007 signature, no second actor argument):

    resolve_gate(task_id, *, decision, notes="", payload=None,
                 decider_did, on_behalf_of=None, actor_roles)

``decider_did`` is who decides: the route passes the on-behalf-of person when an
app acts for one, else the principal DID (SPEC-087 ``acting_identity``), so it
replaces today's ``actor_did`` rather than sitting beside it. ``actor_roles``
keeps today's meaning. A resolved row's ``output`` is the envelope
``{decision, notes, payload, decided_by, on_behalf_of, decided_at, timed_out}``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from arcteam.workflow import GateNotAuthorizedError

from ._human_steps import (
    ALICE,
    APPROVAL_SCHEMA,
    BOB,
    CAROL,
    ENVELOPE_KEYS,
    MALLORY,
    PLANNER,
    PROMISE_SCHEMA,
    TIMEOUT_S,
    Clock,
    Harness,
    actions,
    approval_flow,
    input_flow,
    issue_codes,
    issue_fields,
)
from .conftest import RUNNER_DID, task_id


def harness(backend: Any, tmp_path: Path, definition: Any) -> Harness:
    return Harness(backend=backend, definition=definition, root=tmp_path, clock=Clock())


async def decide(
    h: Harness,
    run_id: str,
    node_id: str,
    decision: str,
    *,
    payload: Any = None,
    decider: str = ALICE,
    on_behalf_of: str | None = None,
    roles: frozenset[str] = PLANNER,
    notes: str = "",
) -> Any:
    return await h.plane.resolve_gate(
        task_id(run_id, node_id, 0),
        decision=decision,
        notes=notes,
        payload=payload,
        decider_did=decider,
        on_behalf_of=on_behalf_of,
        actor_roles=roles,
    )


# -- materialization -----------------------------------------------------------


async def test_gate_row_carries_the_resolved_payload_schema(backend: Any, tmp_path: Path) -> None:
    """The runner stamps the schema OBJECT so the plane never re-reads bundle files."""
    h = harness(backend, tmp_path, approval_flow())
    run_id = await h.reach("approve")

    gate = await h.row(run_id, "approve")

    assert gate.status == "review"
    assert gate.metadata.get("output_schema_resolved") == APPROVAL_SCHEMA


async def test_human_input_materializes_as_a_waiting_review_row(
    backend: Any, tmp_path: Path
) -> None:
    h = harness(backend, tmp_path, input_flow())
    run_id = await h.reach("ask_due")

    rows = await h.rows(run_id)
    row = rows.get(task_id(run_id, "ask_due", 0))

    assert row is not None, "the human_input node never materialized"
    assert row.status == "review"
    assert row.requires_review is True
    assert row.metadata["node_kind"] == "human_input"
    assert row.metadata.get("prompt") == "Confirm the promised date"
    assert row.metadata.get("output_schema_resolved") == PROMISE_SCHEMA
    assert await h.run_status(run_id) == "waiting_gate"
    # Nothing downstream ran while a person is still being asked.
    assert task_id(run_id, "confirm", 0) not in rows


# -- gate payload ----------------------------------------------------------------


async def test_approve_without_required_payload_is_refused_and_keeps_waiting(
    backend: Any, tmp_path: Path
) -> None:
    h = harness(backend, tmp_path, approval_flow())
    run_id = await h.reach("approve")

    result = await decide(h, run_id, "approve", "approve", payload=None)

    assert not result.ok
    assert "invalid_payload" in issue_codes(result)
    gate = await h.row(run_id, "approve")
    assert gate.status == "review", "a refused decision settled the gate"
    assert not gate.output
    assert task_id(run_id, "order", 0) not in await h.rows(run_id)


async def test_approve_with_a_wrongly_typed_field_names_that_field(
    backend: Any, tmp_path: Path
) -> None:
    h = harness(backend, tmp_path, approval_flow())
    run_id = await h.reach("approve")

    result = await decide(h, run_id, "approve", "approve", payload={"qty": "three"})

    assert not result.ok
    assert "invalid_payload" in issue_codes(result)
    assert any(f.endswith("qty") for f in issue_fields(result)), issue_fields(result)
    assert (await h.row(run_id, "approve")).status == "review"


async def test_payload_over_64_kib_is_refused_even_under_an_open_schema(
    backend: Any, tmp_path: Path
) -> None:
    h = harness(backend, tmp_path, approval_flow(output_schema="schemas/open.json"))
    run_id = await h.reach("approve")

    result = await decide(h, run_id, "approve", "approve", payload={"pad": "x" * (65 * 1024)})

    assert not result.ok
    assert (await h.row(run_id, "approve")).status == "review"


async def test_valid_approve_records_the_envelope_with_the_on_behalf_of_person(
    backend: Any, tmp_path: Path
) -> None:
    """An app acting for Alice: Alice decides, and Alice is recorded (REQ-613)."""
    h = harness(backend, tmp_path, approval_flow())
    run_id = await h.reach("approve")

    result = await decide(
        h,
        run_id,
        "approve",
        "approve",
        payload={"qty": 3},
        decider=ALICE,
        on_behalf_of=ALICE,
        notes="ship it",
    )

    assert result.ok, result.errors
    gate = await h.row(run_id, "approve")
    assert gate.status == "done"
    envelope = gate.output or {}
    assert set(envelope) == ENVELOPE_KEYS
    assert envelope["decision"] == "approved"
    assert envelope["notes"] == "ship it"
    assert envelope["payload"] == {"qty": 3}
    assert envelope["decided_by"] == ALICE
    assert envelope["on_behalf_of"] == ALICE
    assert envelope["timed_out"] is False
    decided_at = datetime.fromisoformat(envelope["decided_at"])
    assert decided_at.tzinfo is not None
    [resolved] = [e for e in actions(h.sink, "workflow.gate.resolved") if e.actor_did == ALICE]
    assert resolved.extra.get("on_behalf_of") == ALICE


async def test_direct_decision_records_the_decider_and_no_on_behalf_of(
    backend: Any, tmp_path: Path
) -> None:
    h = harness(backend, tmp_path, approval_flow())
    run_id = await h.reach("approve")

    result = await decide(h, run_id, "approve", "approve", payload={"qty": 1}, decider=BOB)

    assert result.ok, result.errors
    envelope = (await h.row(run_id, "approve")).output or {}
    assert envelope["decided_by"] == BOB
    assert envelope["on_behalf_of"] is None
    assert envelope["decided_by"] != "did:arc:ui:operator"


async def test_downstream_tool_arg_resolves_the_approved_payload_int(
    backend: Any, tmp_path: Path
) -> None:
    h = harness(backend, tmp_path, approval_flow())
    run_id = await h.reach("approve")

    result = await decide(h, run_id, "approve", "approve", payload={"qty": 3})

    assert result.ok, result.errors
    order = await h.row(run_id, "order")
    assert order.metadata["args"] == {"qty": 3}
    assert type(order.metadata["args"]["qty"]) is int


async def test_fail_run_needs_no_payload_even_when_a_schema_is_declared(
    backend: Any, tmp_path: Path
) -> None:
    h = harness(backend, tmp_path, approval_flow())
    run_id = await h.reach("approve")

    result = await decide(h, run_id, "approve", "fail_run", notes="no")

    assert result.ok, result.errors
    gate = await h.row(run_id, "approve")
    assert gate.status == "failed"
    assert (gate.output or {}).get("decision") == "rejected"
    assert await h.run_status(run_id) == "failed"


async def test_gate_without_a_schema_approves_with_an_empty_payload(
    backend: Any, tmp_path: Path
) -> None:
    """Narrowness: a gate that declares no schema keeps working with no payload."""
    h = harness(backend, tmp_path, approval_flow(output_schema=None))
    run_id = await h.reach("approve")

    result = await decide(h, run_id, "approve", "approve")

    assert result.ok, result.errors
    envelope = (await h.row(run_id, "approve")).output or {}
    assert envelope.get("decision") == "approved"
    assert envelope.get("payload") == {}


async def test_submit_on_a_gate_is_refused(backend: Any, tmp_path: Path) -> None:
    h = harness(backend, tmp_path, approval_flow())
    run_id = await h.reach("approve")

    result = await decide(h, run_id, "approve", "submit", payload={"qty": 3})

    assert not result.ok
    gate = await h.row(run_id, "approve")
    assert gate.status == "review"
    assert not gate.output


# -- human_input -----------------------------------------------------------------


async def test_human_input_submit_records_submitted_and_advances(
    backend: Any, tmp_path: Path
) -> None:
    h = harness(backend, tmp_path, input_flow())
    run_id = await h.reach("ask_due")

    result = await decide(h, run_id, "ask_due", "submit", payload={"promised": "2026-11-01"})

    assert result.ok, result.errors
    row = await h.row(run_id, "ask_due")
    assert row.status == "done"
    envelope = row.output or {}
    assert set(envelope) == ENVELOPE_KEYS
    assert envelope["decision"] == "submitted"
    assert envelope["payload"] == {"promised": "2026-11-01"}
    assert envelope["decided_by"] == ALICE
    assert task_id(run_id, "confirm", 0) in await h.rows(run_id)


async def test_human_input_submit_with_invalid_payload_keeps_waiting(
    backend: Any, tmp_path: Path
) -> None:
    h = harness(backend, tmp_path, input_flow())
    run_id = await h.reach("ask_due")

    result = await decide(h, run_id, "ask_due", "submit", payload={"promised": 7})

    assert not result.ok
    assert "invalid_payload" in issue_codes(result)
    assert (await h.row(run_id, "ask_due")).status == "review"


async def test_return_for_revision_on_human_input_is_refused(backend: Any, tmp_path: Path) -> None:
    h = harness(backend, tmp_path, input_flow())
    run_id = await h.reach("ask_due")

    result = await decide(h, run_id, "ask_due", "return_for_revision", notes="again")

    assert not result.ok
    rows = await h.rows(run_id)
    assert rows[task_id(run_id, "ask_due", 0)].status == "review"
    assert task_id(run_id, "draft", 1) not in rows, "a human_input sent work back for revision"


async def test_approve_on_human_input_is_refused(backend: Any, tmp_path: Path) -> None:
    """human_input takes submit or fail_run only (D-845)."""
    h = harness(backend, tmp_path, input_flow())
    run_id = await h.reach("ask_due")

    result = await decide(h, run_id, "ask_due", "approve", payload={"promised": "soon"})

    assert not result.ok
    assert (await h.row(run_id, "ask_due")).status == "review"


async def test_human_input_fail_run_fails_the_run(backend: Any, tmp_path: Path) -> None:
    h = harness(backend, tmp_path, input_flow())
    run_id = await h.reach("ask_due")

    result = await decide(h, run_id, "ask_due", "fail_run", notes="cannot promise")

    assert result.ok, result.errors
    row = await h.row(run_id, "ask_due")
    assert row.status == "failed"
    assert (row.output or {}).get("decision") == "rejected"
    assert await h.run_status(run_id) == "failed"


async def test_unlisted_decider_on_human_input_is_denied(backend: Any, tmp_path: Path) -> None:
    h = harness(backend, tmp_path, input_flow())
    run_id = await h.reach("ask_due")

    with pytest.raises(GateNotAuthorizedError):
        await decide(
            h,
            run_id,
            "ask_due",
            "submit",
            payload={"promised": "2026-11-01"},
            decider=MALLORY,
            roles=frozenset(),
        )

    assert (await h.row(run_id, "ask_due")).status == "review"
    assert actions(h.sink, "workflow.gate.denied")


# -- escalated approver ------------------------------------------------------------


def escalating() -> Any:
    return approval_flow(timeout_s=TIMEOUT_S, on_timeout="escalate", escalate_to=CAROL)


async def mark_escalated(h: Harness, run_id: str, target: str) -> None:
    """Write ``metadata.escalated_to`` the way GateDeadlines does on first expiry."""
    gate = await h.row(run_id, "approve")
    await h.tasks.update(
        gate.id,
        {"metadata": {**gate.metadata, "escalated_to": target}},
        actor_did=RUNNER_DID,
    )


async def test_escalation_target_is_refused_before_escalation(
    backend: Any, tmp_path: Path
) -> None:
    h = harness(backend, tmp_path, escalating())
    run_id = await h.reach("approve")

    with pytest.raises(GateNotAuthorizedError):
        await decide(
            h, run_id, "approve", "approve", payload={"qty": 2}, decider=CAROL, roles=frozenset()
        )
    assert (await h.row(run_id, "approve")).status == "review"


async def test_escalation_target_is_accepted_after_escalation(
    backend: Any, tmp_path: Path
) -> None:
    h = harness(backend, tmp_path, escalating())
    run_id = await h.reach("approve")
    await mark_escalated(h, run_id, CAROL)

    result = await decide(
        h, run_id, "approve", "approve", payload={"qty": 2}, decider=CAROL, roles=frozenset()
    )

    assert result.ok, result.errors
    envelope = (await h.row(run_id, "approve")).output or {}
    assert envelope.get("decided_by") == CAROL


async def test_a_row_naming_an_undeclared_escalation_target_grants_nothing(
    backend: Any, tmp_path: Path
) -> None:
    """Authority comes from the signed ``escalate_to``, never from a row write.

    A forged ``metadata.escalated_to`` naming someone the definition never
    declared must not add an approver (COMP-017: runtime approver add refused).
    """
    h = harness(backend, tmp_path, escalating())
    run_id = await h.reach("approve")
    await mark_escalated(h, run_id, MALLORY)

    with pytest.raises(GateNotAuthorizedError):
        await decide(
            h,
            run_id,
            "approve",
            "approve",
            payload={"qty": 2},
            decider=MALLORY,
            roles=frozenset(),
        )
    assert (await h.row(run_id, "approve")).status == "review"
