"""P14-B step 3 — infrastructure errors never fail a run; lease loss backs off.

A NATS timeout, a fenced write, a store ``OSError``: none of these says anything
about the workflow, so none of them may terminate a run. They are retried every
tick, and a run stuck behind one for 20 consecutive ticks mails the operator
exactly once. A definition error is different: it fails the same way every
time, so it still terminates the run with its reason.
"""

from __future__ import annotations

from typing import Any

import pytest

from arcteam.types import DeliveryKind
from arcteam.workflow.narrator import RunNarrator
from arcteam.workflow.runner import NodeDecisionError

from .conftest import RUNNER_DID, Definition, Node, RecordingSender, RecordingSink
from .test_runner_frontier import build

WIRED = Definition(id="wired", nodes=(Node(id="only", kind="agent", agent="@sales"),))

_NatsTimeout = type("TimeoutError", (Exception,), {"__module__": "nats.errors"})


def _operator_mails(sender: RecordingSender) -> list[Any]:
    return [
        m
        for m in sender.sent
        if "user://operator" in m.to and m.delivery_kind is DeliveryKind.MAIL
    ]


@pytest.mark.parametrize(
    "error",
    [TimeoutError("nats: timeout"), _NatsTimeout("nats: timeout"), OSError("store gone")],
    ids=["timeout", "nats-module", "oserror"],
)
async def test_nats_timeout_does_not_terminate_run_and_mails_after_threshold(
    stores: Any, registry: Any, error: Exception
) -> None:
    _, runs, _ = stores
    sender = RecordingSender()
    sink = RecordingSink()
    runner = build(
        stores,
        registry,
        WIRED,
        audit_sink=sink,
        narrator=RunNarrator(sender, sender_did=RUNNER_DID),
    )
    started = await runner.start_run(
        "wired", input={}, initiator="operator", initiator_did="did:arc:x/1"
    )

    async def infra_down(_: str) -> Any:
        raise error

    runner.advance = infra_down  # type: ignore[method-assign]
    for _ in range(20):
        await runner.tick()

    assert (await runs.get(started.run_id)).status == "running"
    mails = _operator_mails(sender)
    assert len(mails) == 1
    assert started.run_id in (mails[0].subject or "")
    assert "cannot advance" in (mails[0].subject or "")
    assert type(error).__name__ in mails[0].body and "20" in mails[0].body
    outcomes = {e.outcome for e in sink.events if e.action == "workflow.run.advance_failed"}
    assert outcomes == {"retrying"}

    await runner.tick()  # the 21st: still retrying, never a second mail
    assert len(_operator_mails(sender)) == 1
    assert (await runs.get(started.run_id)).status == "running"


async def test_definition_error_still_terminates_with_reason(stores: Any, registry: Any) -> None:
    _, runs, _ = stores
    runner = build(stores, registry, WIRED)
    started = await runner.start_run(
        "wired", input={}, initiator="operator", initiator_did="did:arc:x/1"
    )

    async def broken_definition(_: str) -> Any:
        raise NodeDecisionError("only", "unknown agent '@ghost'")

    runner.advance = broken_definition  # type: ignore[method-assign]
    for _ in range(19):
        await runner.tick()
    assert (await runs.get(started.run_id)).status == "running"
    await runner.tick()

    failed = await runs.get(started.run_id)
    assert failed.status == "failed"
    assert "unknown agent '@ghost'" in (failed.last_error or "")


class _FlakyLease:
    """Holds the lease only when told to."""

    def __init__(self) -> None:
        self.held = False
        self.fence = None

    async def acquire_or_renew(self) -> Any:
        return object() if self.held else None

    async def release(self) -> None:
        return None


async def test_lease_loss_backs_off_without_failing_runs(stores: Any, registry: Any) -> None:
    _, runs, _ = stores
    sink = RecordingSink()
    owner = build(stores, registry, WIRED)
    started = await owner.start_run(
        "wired", input={}, initiator="operator", initiator_did="did:arc:x/1"
    )
    lease = _FlakyLease()
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    runner = build(stores, registry, WIRED, audit_sink=sink, lease=lease, sleep=record_sleep)
    before = await runs.get(started.run_id)

    assert [await runner.tick() for _ in range(3)] == [0, 0, 0]
    assert sleeps == [1, 2, 4]
    after = await runs.get(started.run_id)
    assert after.status == "running" and after.path_taken == before.path_taken
    assert not any(e.action == "workflow.run.advance_failed" for e in sink.events)
    assert sum(e.action == "workflow.runner.lease_unavailable" for e in sink.events) == 3

    lease.held = True
    assert await runner.tick() == 1
    lease.held = False
    await runner.tick()
    assert sleeps == [1, 2, 4, 1], "a successful lease resets the back-off"


async def test_backoff_is_capped_at_sixty_seconds(stores: Any, registry: Any) -> None:
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    runner = build(stores, registry, WIRED, lease=_FlakyLease(), sleep=record_sleep)
    for _ in range(9):
        await runner.tick()
    assert sleeps[-1] == 60 and max(sleeps) == 60
