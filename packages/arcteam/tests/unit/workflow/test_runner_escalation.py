"""SPEC-061 — a tick that never stops failing must not look like a healthy idle one.

``run_forever``'s per-run isolation (inside ``tick()``) is proven separately by
``test_one_poisoned_run_does_not_stop_the_tick_for_the_others`` and must stay
untouched. This file covers the level above it: ``tick()`` itself can still
raise (its call to list active runs sits outside the per-run guard), and
nothing used to escalate when that kept happening — a log line every interval
is indistinguishable, from the outside, from a runner making progress.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend
from arcstore.mutation_fence import (
    RUNNER_LEASE_COLLECTION,
    RUNNER_LEASE_KEY,
    MutationFenceRejectedError,
    RunnerFence,
)

from arcteam.workflow.narrator import RunNarrator
from arcteam.workflow.runner import NodeDecisionError
from arcteam.workflow.stores import WorkflowRunStore

from .conftest import RUNNER_DID, Definition, Node, RecordingSender, RecordingSink
from .test_runner_frontier import build

CHANNEL = "channel://escalation"

WIRED = Definition(
    id="wired",
    channel=CHANNEL,
    nodes=(Node(id="only", kind="agent", agent="@sales"),),
)


class FlakyRunStore:
    """Wraps a real run store; ``active_runs`` can be told to always raise.

    Stands in for the store connection actually dropping — the one call in
    ``tick()`` that sits OUTSIDE the per-run try/except, so it is the only way
    a whole ``tick()`` call can raise.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.poisoned = False

    async def active_runs(self) -> Any:
        if self.poisoned:
            raise RuntimeError("store connection lost")
        return await self._inner.active_runs()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def _cancel(task: asyncio.Task[None]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_repeated_tick_failure_is_escalated_via_audit_and_narration(
    stores: Any, registry: Any
) -> None:
    """The threshold is the REQ'd behavior: 3 consecutive whole-tick failures page.

    A tick that raises once is noise (a transient hiccup); this proves the 3rd
    consecutive one produces an operator-visible audit event carrying the
    failure count and the last error, plus a channel post since a narrator is
    wired — not merely another log line.
    """
    _, runs, _ = stores
    flaky = FlakyRunStore(runs)
    sink = RecordingSink()
    sender = RecordingSender()
    narrator = RunNarrator(sender, sender_did=RUNNER_DID)
    runner = build(stores, registry, WIRED, audit_sink=sink, narrator=narrator)
    runner._runs = flaky  # swap in the flaky wrapper after construction

    # One healthy tick first, so the runner has actually seen the run's bound
    # channel before the store goes flaky — proving escalation narrates to a
    # channel learned from real activity, not a hardcoded one. ``start_run``
    # itself only calls ``advance``, never ``tick``, so this needs an explicit
    # tick to populate the cache.
    await runner.start_run("wired", input={}, initiator_did="did:arc:x/1")
    await runner.tick()
    assert runner._last_known_channels == [CHANNEL]

    flaky.poisoned = True
    task = asyncio.create_task(runner.run_forever(interval=0.005))
    try:
        for _ in range(400):
            if any(e.action == "workflow.runner.degraded" for e in sink.events):
                break
            await asyncio.sleep(0.005)
        else:
            pytest.fail("escalation audit event never fired within the timeout")
    finally:
        await _cancel(task)

    degraded = [e for e in sink.events if e.action == "workflow.runner.degraded"]
    assert degraded[0].extra["consecutive_failures"] == 3, (
        "the FIRST escalation must land at exactly the threshold, not before or after"
    )
    assert "store connection lost" in degraded[0].extra["last_error"]

    narrated = [m for m in sender.sent if m.meta.get("event") == "runner.degraded"]
    assert narrated, "a wired narrator must post the escalation to a channel"
    assert narrated[0].to == [CHANNEL]


async def test_a_successful_tick_resets_the_consecutive_failure_count(
    stores: Any, registry: Any
) -> None:
    """Two failures, a success, then two more must NOT reach the threshold.

    Mirrors the scheduler's circuit breaker: consecutive_failures is reset by
    any successful tick, not accumulated across an interruption.
    """
    sink = RecordingSink()
    runner = build(stores, registry, WIRED, audit_sink=sink)

    script: list[BaseException | None] = [
        RuntimeError("blip 1"),
        RuntimeError("blip 2"),
        None,  # a real, successful tick — must reset the streak
        RuntimeError("blip 3"),
        RuntimeError("blip 4"),
    ]
    calls = {"n": 0}

    async def scripted_tick() -> int:
        i = calls["n"]
        calls["n"] += 1
        # Anything beyond the script pads with success, so a generous test
        # timeout can never accidentally manufacture a 3rd consecutive
        # failure that the script itself never intended.
        outcome = script[i] if i < len(script) else None
        if outcome is not None:
            raise outcome
        return 0

    runner.tick = scripted_tick  # type: ignore[method-assign]
    task = asyncio.create_task(runner.run_forever(interval=0.005))
    await asyncio.sleep(0.005 * (len(script) + 5))
    await _cancel(task)

    assert calls["n"] >= len(script), "the loop must have run through the whole script"
    degraded = [e for e in sink.events if e.action == "workflow.runner.degraded"]
    assert degraded == [], (
        "no run of 3 CONSECUTIVE failures ever occurred — the success in the "
        "middle must have reset the counter, or this would have escalated"
    )


async def test_one_poisoned_run_still_does_not_stop_the_tick(stores: Any, registry: Any) -> None:
    """The pre-existing per-run isolation this change must not weaken.

    A run whose ``advance`` always raises must not prevent other runs from
    advancing, tick after tick, and must not by itself trip the new
    whole-tick escalation — that escalation is for ``tick()`` raising, and
    the per-run guard means a poisoned run alone never makes it do that.
    """
    sink = RecordingSink()
    runner = build(stores, registry, WIRED, audit_sink=sink)

    async def always_raise(run_id: str) -> Any:
        raise RuntimeError("this run is poisoned")

    await runner.start_run("wired", input={}, initiator_did="did:arc:x/1")
    runner.advance = always_raise  # type: ignore[method-assign]

    task = asyncio.create_task(runner.run_forever(interval=0.005))
    await asyncio.sleep(0.005 * 8)
    await _cancel(task)

    degraded = [e for e in sink.events if e.action == "workflow.runner.degraded"]
    assert degraded == [], "a poisoned RUN must not trip the whole-tick escalation"


async def test_poisoned_run_is_terminalized_after_bounded_advance_failures(
    stores: Any, registry: Any
) -> None:
    """A deterministic per-run fault must not be retried forever on every tick."""
    _, runs, _ = stores
    sink = RecordingSink()
    runner = build(stores, registry, WIRED, audit_sink=sink, advance_failure_threshold=3)
    started = await runner.start_run("wired", input={}, initiator_did="did:arc:x/1")

    async def poisoned(_: str) -> Any:
        raise NodeDecisionError("only", "corrupt companion state")

    runner.advance = poisoned  # type: ignore[method-assign]
    await runner.tick()
    await runner.tick()
    await runner.tick()

    terminal = await runs.get(started.run_id)
    assert terminal is not None
    assert terminal.status == "failed"
    failures = [event for event in sink.events if event.action == "workflow.run.advance_failed"]
    assert [event.outcome for event in failures] == ["retrying", "retrying", "terminalized"]
    assert failures[-1].extra["consecutive_failures"] == 3


async def test_poisoned_run_failure_budget_survives_runner_restart(
    stores: Any, registry: Any
) -> None:
    """Restarting the runner cannot grant a poisoned run endless new attempts."""
    _, runs, _ = stores
    first = build(stores, registry, WIRED, advance_failure_threshold=3)
    poisoned = await first.start_run("wired", input={}, initiator_did="did:arc:x/1")
    healthy = await first.start_run("wired", input={}, initiator_did="did:arc:x/1")

    async def fail_one(run_id: str) -> Any:
        if run_id == poisoned.run_id:
            raise NodeDecisionError("only", "corrupt companion state")
        return await original(run_id)

    for _ in range(3):
        runner = build(stores, registry, WIRED, advance_failure_threshold=3)
        original = runner.advance
        runner.advance = fail_one  # type: ignore[method-assign]
        await runner.tick()

    assert (await runs.get(poisoned.run_id)).status == "failed"
    assert (await runs.get(healthy.run_id)).status == "running"


async def test_real_run_store_resets_failure_budget_only_after_progress() -> None:
    backend = FakeBackend()
    store = WorkflowRunStore(backend)
    await store.create_run(
        run_id="run-poison",
        workflow_id="wired",
        version=1,
        content_hash="sha256:test",
        initiator_did="did:arc:x/1",
        channel=None,
        input={},
        budget_tokens=None,
        budget_cost_usd=None,
        budget_wall_clock_s=None,
    )
    assert await store.record_advance_failure("run-poison", actor_did=RUNNER_DID) == 1
    assert await store.record_advance_failure("run-poison", actor_did=RUNNER_DID) == 2
    restarted = WorkflowRunStore(backend)
    assert await restarted.record_advance_failure("run-poison", actor_did=RUNNER_DID) == 3
    await backend.mutable_merge(
        "workflow_run_state", "run-poison", {"path_len": 1}, actor_did=RUNNER_DID
    )
    assert await restarted.record_advance_failure("run-poison", actor_did=RUNNER_DID) == 1


async def test_stale_runner_fence_cannot_increment_durable_failure_count() -> None:
    backend = FakeBackend()
    store = WorkflowRunStore(backend)
    await store.create_run(
        run_id="run-fenced",
        workflow_id="wired",
        version=1,
        content_hash="sha256:test",
        initiator_did="did:arc:x/1",
        channel=None,
        input={},
        budget_tokens=None,
        budget_cost_usd=None,
        budget_wall_clock_s=None,
    )
    expiry = datetime.now(UTC) + timedelta(minutes=1)
    await backend.mutable_write(
        RUNNER_LEASE_COLLECTION,
        RUNNER_LEASE_KEY,
        {"owner_id": "new-owner", "fencing_token": 2, "expires_at": expiry.isoformat()},
        actor_did=RUNNER_DID,
    )
    with pytest.raises(MutationFenceRejectedError):
        await store.record_advance_failure(
            "run-fenced",
            actor_did=RUNNER_DID,
            fence=RunnerFence(owner_id="old-owner", token=1, expires_at=expiry),
        )
    assert (
        await store.record_advance_failure(
            "run-fenced",
            actor_did=RUNNER_DID,
            fence=RunnerFence(owner_id="new-owner", token=2, expires_at=expiry),
        )
        == 1
    )


async def test_uncertain_advance_errors_never_claim_known_terminal_failure(
    stores: Any, registry: Any
) -> None:
    _, runs, _ = stores
    sink = RecordingSink()
    runner = build(stores, registry, WIRED, audit_sink=sink, advance_failure_threshold=2)
    started = await runner.start_run("wired", input={}, initiator_did="did:arc:x/1")

    async def unknown(_: str) -> Any:
        raise RuntimeError("database response lost after task materialization")

    runner.advance = unknown  # type: ignore[method-assign]
    await runner.tick()
    await runner.tick()
    assert (await runs.get(started.run_id)).status == "running"
    assert not any(
        event.outcome == "terminalized"
        for event in sink.events
        if event.action == "workflow.run.advance_failed"
    )


async def test_tracking_error_on_poisoned_run_does_not_starve_peer(
    stores: Any, registry: Any
) -> None:
    _, runs, _ = stores
    sink = RecordingSink()
    runner = build(stores, registry, WIRED, audit_sink=sink)
    poisoned = await runner.start_run("wired", input={}, initiator_did="did:arc:x/1")
    healthy = await runner.start_run("wired", input={}, initiator_did="did:arc:x/1")
    original_advance = runner.advance
    original_record = runs.record_advance_failure
    seen: list[str] = []

    async def advance(run_id: str) -> Any:
        seen.append(run_id)
        if run_id == poisoned.run_id:
            raise NodeDecisionError("only", "missing companion state")
        return await original_advance(run_id)

    async def record(run_id: str, **kwargs: Any) -> int:
        if run_id == poisoned.run_id:
            raise RuntimeError("companion state missing")
        return await original_record(run_id, **kwargs)

    runner.advance = advance  # type: ignore[method-assign]
    runs.record_advance_failure = record  # type: ignore[method-assign]
    await runner.tick()
    assert healthy.run_id in seen
    assert any(event.action == "workflow.run.advance_tracking_failed" for event in sink.events)


async def test_lost_materialization_response_restarts_without_duplicate_node(
    stores: Any, registry: Any
) -> None:
    tasks, runs, _ = stores
    first = build(stores, registry, WIRED)
    started = await first.start_run("wired", input={}, initiator_did="did:arc:x/1", detached=True)
    original_create = tasks.create_batch
    lost = False

    async def create_then_lose_response(*args: Any, **kwargs: Any) -> Any:
        nonlocal lost
        result = await original_create(*args, **kwargs)
        if not lost:
            lost = True
            raise RuntimeError("task commit response lost")
        return result

    tasks.create_batch = create_then_lose_response  # type: ignore[method-assign]
    await first.tick()
    tasks.create_batch = original_create  # type: ignore[method-assign]
    restarted = build(stores, registry, WIRED)
    await restarted.tick()
    rows = await tasks.query_by_flow_run(started.run_id)
    assert len(rows) == 1
    assert tasks.created_keys.count(rows[0].id) == 1
    assert (await runs.get(started.run_id)).status == "running"


async def test_real_missing_companion_state_does_not_starve_peer(
    stores: Any, registry: Any
) -> None:
    tasks, _, task_store = stores
    backend = tasks._backend
    runs = WorkflowRunStore(backend)
    runner = build((tasks, runs, task_store), registry, WIRED)
    poisoned = await runner.start_run("wired", input={}, initiator_did="did:arc:x/1")
    healthy = await runner.start_run("wired", input={}, initiator_did="did:arc:x/1")
    await backend.mutable_delete("workflow_run_state", poisoned.run_id, actor_did=RUNNER_DID)
    seen: list[str] = []
    original = runner.advance

    async def advance(run_id: str) -> Any:
        seen.append(run_id)
        if run_id == poisoned.run_id:
            raise NodeDecisionError("only", "missing companion state")
        return await original(run_id)

    runner.advance = advance  # type: ignore[method-assign]
    await runner.tick()
    assert healthy.run_id in seen
