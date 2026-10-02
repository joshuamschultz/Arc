"""A retry that may repeat a side effect needs an explicit, audited operator accept.

The executing agent refuses to re-run a non-idempotent tool unless the new
attempt row carries ``operator_retry_ok``. Nothing used to set it, so the
run-view checkbox was theatre. The control plane now sets it only when the
operator says so, and records that they did.
"""

from __future__ import annotations

from typing import Any

from .conftest import task_id
from .test_control_plane_retry import OPERATOR, _failed_run, _plane


def _retried_events(sink: Any) -> list[Any]:
    return [e for e in sink.events if e.action == "workflow.node.retried" and e.outcome == "retried"]


async def test_retry_marks_the_new_attempt_as_an_operator_retry(
    stores: Any, registry: Any
) -> None:
    _, _, tasks = stores
    control, sink = _plane(stores, registry)
    run_id = await _failed_run(control, stores)

    result = await control.retry_node(run_id, "b", actor_did=OPERATOR)

    assert result.ok, result.errors
    row = await tasks.get(task_id(run_id, "b", 1))
    assert row.metadata.get("operator_retry") is True, "the executor must know this is a re-run"
    assert not row.metadata.get("operator_retry_ok"), "a repeat is not accepted by default"
    assert _retried_events(sink)[-1].extra["accepted_repeat"] is False


async def test_accept_side_effect_repeat_sets_operator_retry_ok_and_audits_it(
    stores: Any, registry: Any
) -> None:
    _, _, tasks = stores
    control, sink = _plane(stores, registry)
    run_id = await _failed_run(control, stores)

    result = await control.retry_node(
        run_id, "b", actor_did=OPERATOR, accept_side_effect_repeat=True
    )

    assert result.ok, result.errors
    row = await tasks.get(task_id(run_id, "b", 1))
    assert row.metadata["operator_retry_ok"] is True
    event = _retried_events(sink)[-1]
    assert event.extra["accepted_repeat"] is True
    assert event.actor_did == OPERATOR


async def test_a_refused_retry_never_sets_the_accept(stores: Any, registry: Any) -> None:
    _, _, tasks = stores
    control, _ = _plane(stores, registry)
    run_id = await _failed_run(control, stores)

    refused = await control.retry_node(
        run_id, "a", actor_did=OPERATOR, accept_side_effect_repeat=True
    )

    assert not refused.ok
    assert await tasks.get(task_id(run_id, "a", 1)) is None
