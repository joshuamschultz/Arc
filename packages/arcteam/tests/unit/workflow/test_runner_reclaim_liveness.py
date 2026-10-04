"""Resume must never reclaim a node that is still alive.

``RunnerHost`` calls ``resume()`` after every runner rebuild. A node with no
timeout used to be taken back after the 900 s floor even when it was still
running in the SAME process, so a 35-minute meeting-ingest node was killed and
retried. Liveness is now proved by the attempt's lease (owner process + beat),
not guessed from the wall clock. Time is a fake clock the test advances; the
node's "work" is parked on an ``asyncio.Event`` so every resume lands while it
is genuinely mid-run.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from arcstore.backends.memory import FakeBackend
from arcstore.tasks import (
    ATTEMPT_LEASE_BEAT_S,
    ATTEMPT_LEASE_TTL_S,
    NODE_TIMEOUT_EXCEEDED,
    PROCESS_INSTANCE_ID,
    RECLAIM_MARGIN_S,
    SERVICE_RESTART_INTERRUPTED,
)

from arcteam.workflow.runner import WorkflowRunner
from arcteam.workflow.stores import WorkflowRunStore, WorkflowTaskStore

from .conftest import RUNNER_DID, SALES_DID, Bundle, evaluate, resolve_args, task_id
from .test_runner_frontier import FakeDefinitions
from .test_runner_resume import Registry, World, attempt_key

START = datetime(2026, 10, 4, 3, 0, tzinfo=UTC)
FLOOR_S = 900.0


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def build_runner(world: World, clock: FakeClock) -> WorkflowRunner:
    """A runner rebuilt over the same durable rows, as RunnerHost does."""
    return WorkflowRunner(
        tasks=WorkflowTaskStore(world.backend, actor_did=RUNNER_DID, clock=clock),
        runs=WorkflowRunStore(world.backend),
        definitions=FakeDefinitions(Bundle(world.definition)),
        owners=Registry(),
        runner_did=RUNNER_DID,
        tier="personal",
        evaluate=evaluate,
        resolve_args=resolve_args,
        reclaim_after_s=FLOOR_S,
        clock=clock,
    )


async def claim_node(
    world: World, run_id: str, clock: FakeClock, *, owner: str, timeout: float | None = None
):
    """The agent claims node ``one`` at the clock's now, stamping its lease."""
    one = task_id(run_id, "one")
    row = await world.tasks.get(one)
    assert row is not None
    if timeout is not None:
        await world.tasks.update(one, {"timeout_seconds": timeout}, actor_did=SALES_DID)
    claimed, _ = await world.tasks.start_task(
        one, SALES_DID, attempt_key=attempt_key(row, 1), lease_owner=owner
    )
    assert claimed is not None and claimed.status == "in_progress"
    # Re-anchor the claim to the fake clock: started_at and the first beat.
    stamp = clock().isoformat()
    await world.tasks.update(
        one, {"started_at": stamp, "lease_beat_at": stamp}, actor_did=SALES_DID
    )
    return await world.tasks.get(one)


async def start(world: World, run_id: str) -> None:
    await world.new_runner().start_run(
        "chain3", input={}, initiator="operator", initiator_did="did:arc:x/1", run_id=run_id
    )


async def test_twenty_minute_node_survives_runner_rebuilds_and_resume() -> None:
    backend = FakeBackend()
    await backend.start()
    world = World(backend)
    clock = FakeClock()
    await start(world, "run-long")
    row = await claim_node(world, "run-long", clock, owner=PROCESS_INSTANCE_ID)
    assert row is not None

    release = asyncio.Event()
    beats = 0

    async def node_work() -> None:
        """Heartbeats on the fake clock until released, like the agent's beat loop."""
        nonlocal beats
        while not release.is_set():
            await asyncio.sleep(0)
            if clock().timestamp() - last_beat[0] >= ATTEMPT_LEASE_BEAT_S:
                ok = await world.tasks.beat_attempt(
                    row.id,
                    attempts=row.attempts,
                    lease_owner=PROCESS_INSTANCE_ID,
                    actor_did=SALES_DID,
                    now=clock(),
                )
                assert ok
                last_beat[0] = clock().timestamp()
                beats += 1

    last_beat = [clock().timestamp()]
    work = asyncio.create_task(node_work())
    for minute in range(20):  # twenty minutes, one resume per simulated minute
        clock.advance(60)
        await asyncio.sleep(0)  # the node's heartbeat runs before the resume
        await asyncio.sleep(0)
        await build_runner(world, clock).resume()  # a rebuilt runner each time
        live = await world.tasks.get(row.id)
        assert live is not None and live.status == "in_progress", f"reclaimed at minute {minute}"
        assert "reclaimed_at" not in live.metadata
    release.set()
    await work
    assert beats == 20, "the node renewed its lease every simulated minute"

    done = await world.tasks.complete_attempt(
        row.id,
        attempt_key=attempt_key(row, 1),
        attempts=row.attempts,
        resolution="node complete",
        output={},
        actor_did=SALES_DID,
    )
    assert done is not None, "the original attempt is still the live one and can finish"


async def test_node_whose_owner_died_is_reclaimed_as_service_restart() -> None:
    backend = FakeBackend()
    await backend.start()
    world = World(backend)
    clock = FakeClock()
    await start(world, "run-dead")
    await claim_node(world, "run-dead", clock, owner="999:previous-process")

    # Well inside the 900 s floor: the old guess would have waited. The owner
    # stopped beating, so its lease lapses after the TTL.
    clock.advance(ATTEMPT_LEASE_TTL_S + 1)
    await build_runner(world, clock).resume()

    row = await world.tasks.get(task_id("run-dead", "one"))
    assert row is not None and row.status == "todo"
    assert (row.last_error or "").startswith(SERVICE_RESTART_INTERRUPTED)
    assert row.lease_owner is None


async def test_foreign_owner_with_a_fresh_beat_is_left_alone_and_rechecked() -> None:
    backend = FakeBackend()
    await backend.start()
    world = World(backend)
    clock = FakeClock()
    await start(world, "run-foreign")
    row = await claim_node(world, "run-foreign", clock, owner="999:other-live-process")

    clock.advance(ATTEMPT_LEASE_BEAT_S)
    runner = build_runner(world, clock)
    await runner.resume()

    live = await world.tasks.get(row.id)
    assert live is not None and live.status == "in_progress"
    # Its owner may die before the next restart, so resume comes back for it.
    assert runner._resume_pending is True

    clock.advance(ATTEMPT_LEASE_TTL_S + 1)
    await runner.tick()
    gone = await world.tasks.get(row.id)
    assert gone is not None and gone.status == "todo"
    assert (gone.last_error or "").startswith(SERVICE_RESTART_INTERRUPTED)


async def test_node_past_its_explicit_timeout_is_reclaimed_even_with_a_fresh_beat() -> None:
    backend = FakeBackend()
    await backend.start()
    world = World(backend)
    clock = FakeClock()
    await start(world, "run-timeout")
    row = await claim_node(world, "run-timeout", clock, owner=PROCESS_INSTANCE_ID, timeout=600)
    assert row is not None

    clock.advance(600 + RECLAIM_MARGIN_S - 1)
    await world.tasks.beat_attempt(
        row.id,
        attempts=row.attempts,
        lease_owner=PROCESS_INSTANCE_ID,
        actor_did=SALES_DID,
        now=clock(),
    )
    await build_runner(world, clock).resume()
    inside = await world.tasks.get(row.id)
    assert inside is not None and inside.status == "in_progress", "inside the margin"

    clock.advance(2)
    await world.tasks.beat_attempt(
        row.id,
        attempts=row.attempts,
        lease_owner=PROCESS_INSTANCE_ID,
        actor_did=SALES_DID,
        now=clock(),
    )
    await build_runner(world, clock).resume()
    past = await world.tasks.get(row.id)
    assert past is not None and past.status == "todo"
    assert (past.last_error or "").startswith(NODE_TIMEOUT_EXCEEDED)


async def test_beat_from_a_reclaimed_attempt_is_refused() -> None:
    backend = FakeBackend()
    await backend.start()
    world = World(backend)
    clock = FakeClock()
    await start(world, "run-stale-beat")
    row = await claim_node(world, "run-stale-beat", clock, owner="999:previous-process")
    assert row is not None
    clock.advance(ATTEMPT_LEASE_TTL_S + 1)
    await build_runner(world, clock).resume()

    assert not await world.tasks.beat_attempt(
        row.id, attempts=row.attempts, lease_owner="999:previous-process", actor_did=SALES_DID
    )


async def test_row_with_no_lease_keeps_the_wall_clock_floor() -> None:
    """An executor that never beats (no lease) is still bounded by the floor."""
    backend = FakeBackend()
    await backend.start()
    world = World(backend)
    clock = FakeClock()
    await start(world, "run-nolease")
    one = task_id("run-nolease", "one")
    row = await world.tasks.get(one)
    assert row is not None
    await world.tasks.start_task(one, SALES_DID, attempt_key=attempt_key(row, 1))
    await world.tasks.update(one, {"started_at": clock().isoformat()}, actor_did=SALES_DID)

    clock.advance(FLOOR_S - 1)
    await build_runner(world, clock).resume()
    held = await world.tasks.get(one)
    assert held is not None and held.status == "in_progress"

    clock.advance(2)
    await build_runner(world, clock).resume()
    freed = await world.tasks.get(one)
    assert freed is not None and freed.status == "todo"
