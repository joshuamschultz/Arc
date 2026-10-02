"""P14-B step 3 — crash/restart resume over the REAL run and task stores.

A process can die between any two durable writes. These tests kill it at every
one of them — once just before the write commits and once just after — then
start a fresh runner over the same durable rows, ``resume()``, and drive the run
to the end. Whatever the crash point, the run must finish with exactly one row
per node, exactly one journal entry per materialization, a snapshot that equals
the derived state, and no attempt key whose side effect ran twice.

The "agent" here is the minimal executor contract (P14-B step 2): claim the row
with its attempt key, refuse a key that already has a recorded result, run the
side effect, then record the result guarded on the same attempt.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend
from arcstore.tasks import Task, TaskStore

from arcteam.workflow.runner import WorkflowRunner
from arcteam.workflow.stores import WorkflowRunStore, WorkflowTaskStore

from .conftest import (
    OPS_DID,
    RUNNER_DID,
    SALES_DID,
    Bundle,
    Definition,
    Node,
    evaluate,
    resolve_args,
    task_id,
)
from .test_node_states import reference_node_states
from .test_runner_frontier import FakeDefinitions

CHAIN = Definition(
    id="chain3",
    nodes=(
        Node(id="one", kind="agent", agent="@sales"),
        Node(id="two", kind="agent", agent="@ops", needs=("one",)),
        Node(id="three", kind="agent", agent="@sales", needs=("two",)),
    ),
)
OWNER = {"one": SALES_DID, "two": OPS_DID, "three": SALES_DID}


class Crash(BaseException):
    """The process died. BaseException so no ``except Exception`` can swallow it."""


class CrashingBackend(FakeBackend):
    """Counts every durable mutation and kills the process at one of them."""

    def __init__(self) -> None:
        super().__init__()
        self.mutations = 0
        self.crash_at: int | None = None
        self.mode = "after"

    async def _mutate(self, call: Any) -> Any:
        self.mutations += 1
        hit = self.crash_at is not None and self.mutations == self.crash_at
        if hit and self.mode == "before":
            call.close()  # the write never started
            raise Crash(f"killed before mutation {self.mutations}")
        result = await call
        if hit:
            raise Crash(f"killed after mutation {self.mutations}")
        return result

    async def mutable_write(self, *a: Any, **k: Any) -> Any:
        return await self._mutate(FakeBackend.mutable_write(self, *a, **k))

    async def mutable_merge(self, *a: Any, **k: Any) -> Any:
        return await self._mutate(FakeBackend.mutable_merge(self, *a, **k))

    async def update_if(self, *a: Any, **k: Any) -> Any:
        return await self._mutate(FakeBackend.update_if(self, *a, **k))

    async def mutable_create_batch(self, *a: Any, **k: Any) -> Any:
        return await self._mutate(FakeBackend.mutable_create_batch(self, *a, **k))

    async def append_if_absent(self, *a: Any, **k: Any) -> Any:
        return await self._mutate(FakeBackend.append_if_absent(self, *a, **k))

    async def mutable_increment(self, *a: Any, **k: Any) -> Any:
        return await self._mutate(FakeBackend.mutable_increment(self, *a, **k))

    async def update_if_increment(self, *a: Any, **k: Any) -> Any:
        return await self._mutate(FakeBackend.update_if_increment(self, *a, **k))


class Registry:
    async def resolve_owner(self, handle: str) -> str | None:
        return {"sales": SALES_DID, "ops": OPS_DID}.get(handle.lstrip("@"))


def attempt_key(row: Task, attempts: int) -> str:
    """The executor contract: run + node + iteration + attempt."""
    meta = row.metadata
    return f"{meta['flow_run_id']}:{meta['node_id']}:{meta['iteration']}:{attempts}"


class World:
    def __init__(self, backend: FakeBackend, definition: Definition = CHAIN) -> None:
        self.backend = backend
        self.definition = definition
        self.tasks = TaskStore(backend)
        self.runs = WorkflowRunStore(backend)
        self.flow_tasks = WorkflowTaskStore(backend, actor_did=RUNNER_DID)
        self.side_effects: Counter[str] = Counter()
        self.runner: WorkflowRunner | None = None

    def new_runner(self) -> WorkflowRunner:
        return WorkflowRunner(
            tasks=WorkflowTaskStore(self.backend, actor_did=RUNNER_DID),
            runs=WorkflowRunStore(self.backend),
            definitions=FakeDefinitions(Bundle(self.definition)),
            owners=Registry(),
            runner_did=RUNNER_DID,
            tier="personal",
            evaluate=evaluate,
            resolve_args=resolve_args,
            # The crash took the agent process down with the runner, so any row
            # still in flight at restart has no live run behind it.
            reclaim_after_s=0.0,
        )

    async def execute_ready(self, run_id: str) -> None:
        """Every owning agent claims and runs its ready node once."""
        for row in await self.flow_tasks.query_by_flow_run(run_id):
            if row.status != "todo":
                continue
            owner = OWNER[str(row.metadata["node_id"])]
            key = attempt_key(row, row.attempts + 1)
            started, _ = await self.tasks.start_task(row.id, owner, attempt_key=key)
            if started is None:
                continue
            if started.metadata.get("attempt_result_key") == key:
                continue
            self.side_effects[key] += 1
            await self.tasks.complete_attempt(
                row.id,
                attempt_key=key,
                attempts=started.attempts,
                resolution="node complete",
                output={"node": row.metadata["node_id"]},
                actor_did=owner,
            )

    async def journal(self, run_id: str) -> list[dict[str, Any]]:
        state = await self.backend.mutable_read("workflow_run_state", run_id)
        return list((state or {}).get("path") or [])


async def drive(world: World, run_id: str, *, max_steps: int = 40) -> None:
    """Fire the run, then tick and execute until terminal, restarting after each crash."""
    fired = False
    for _ in range(max_steps):
        try:
            if world.runner is None:
                world.runner = world.new_runner()
                await world.runner.resume()
            if not fired:
                # A re-fire after a crash is the scheduler retrying the same occurrence.
                await world.runner.start_run(
                    world.definition.id,
                    input={},
                    initiator="scheduler",
                    initiator_did="did:arc:x/scheduler",
                    run_id=run_id,
                    detached=True,
                )
                fired = True
            record = await world.runs.get(run_id)
            if record is not None and record.status in ("done", "failed", "cancelled"):
                return
            await world.runner.tick()
            await world.execute_ready(run_id)
        except Crash:
            world.runner = None  # the process is gone; the next step restarts it
    raise AssertionError("run never reached a terminal state")


async def assert_exactly_once(world: World, run_id: str) -> None:
    record = await world.runs.record(run_id)
    assert record is not None and record.status == "done", record
    rows = await world.flow_tasks.query_by_flow_run(run_id)
    assert sorted(r.id for r in rows) == sorted(
        task_id(run_id, node) for node in ("one", "two", "three")
    )
    materialized = [e for e in await world.journal(run_id) if e.get("kind") == "materialized"]
    assert sorted((e["node_id"], e["iteration"]) for e in materialized) == [
        ("one", 0),
        ("three", 0),
        ("two", 0),
    ]
    assert all(count == 1 for count in world.side_effects.values()), world.side_effects
    for row in rows:
        assert world.side_effects[row.metadata["attempt_result_key"]] == 1
    run = await world.runs.get(run_id)
    assert run is not None
    assert dict(run.node_states) == reference_node_states(rows, run.path_taken)
    assert len(await world.runs.list_for_workflow(world.definition.id)) == 1


async def _clean_mutation_count() -> int:
    backend = CrashingBackend()
    await backend.start()
    world = World(backend)
    await drive(world, "run-x")
    await assert_exactly_once(world, "run-x")
    return backend.mutations


@pytest.mark.parametrize("mode", ["before", "after"])
async def test_crash_at_every_durable_write_then_resume_is_exactly_once_per_attempt_key(
    mode: str,
) -> None:
    total = await _clean_mutation_count()
    assert total > 20, "the scenario must exercise materialize, complete and journal writes"
    for crash_at in range(1, total + 1):
        backend = CrashingBackend()
        await backend.start()
        backend.crash_at, backend.mode = crash_at, mode
        world = World(backend)
        await drive(world, "run-x")
        try:
            await assert_exactly_once(world, "run-x")
        except AssertionError as exc:
            raise AssertionError(f"crash {mode} mutation {crash_at}: {exc}") from exc


async def test_restart_between_materialize_and_journal_creates_exactly_one_row() -> None:
    backend = FakeBackend()
    await backend.start()
    world = World(backend)
    first = world.new_runner()
    original = first._tasks.create_batch

    async def create_then_die(*args: Any, **kwargs: Any) -> Any:
        await original(*args, **kwargs)
        raise Crash("killed after create_batch, before the journal append")

    first._tasks.create_batch = create_then_die  # type: ignore[method-assign]
    await first.start_run(
        "chain3",
        input={},
        initiator="operator",
        initiator_did="did:arc:x/1",
        run_id="run-j",
        detached=True,
    )
    with pytest.raises(Crash):
        await first.advance("run-j")

    restarted = world.new_runner()
    await restarted.resume()
    await restarted.advance("run-j")

    rows = await world.flow_tasks.query_by_flow_run("run-j")
    assert [r.id for r in rows] == [task_id("run-j", "one")]
    journal = [e for e in await world.journal("run-j") if e.get("kind") == "materialized"]
    assert [(e["node_id"], e["iteration"]) for e in journal] == [("one", 0)]
    run = await world.runs.get("run-j")
    assert run is not None and run.node_states["one"].status == "materialized"


async def test_restart_resumes_from_last_completed_node_without_rerunning_done_nodes() -> None:
    backend = FakeBackend()
    await backend.start()
    world = World(backend)
    runner = world.new_runner()
    await runner.start_run(
        "chain3", input={}, initiator="operator", initiator_did="did:arc:x/1", run_id="run-r"
    )
    await world.execute_ready("run-r")  # node one done
    await runner.advance("run-r")
    two = task_id("run-r", "two")
    row = await world.tasks.get(two)
    assert row is not None
    started, _ = await world.tasks.start_task(
        two, OPS_DID, attempt_key=attempt_key(row, row.attempts + 1)
    )
    assert started is not None and started.status == "in_progress"
    # Its agent died mid-run long ago: the lease on the attempt has expired.
    stale = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    await backend.mutable_merge("tasks", two, {"started_at": stale}, actor_did=OPS_DID)

    restarted = world.new_runner()
    resumed = await restarted.resume()
    assert resumed == 1
    reclaimed = await world.tasks.get(two)
    assert reclaimed is not None and reclaimed.status == "todo"
    assert reclaimed.attempts == 1, "reclaim never spends an attempt the next claim will count"

    await restarted.advance("run-r")
    ids = [r.id for r in await world.flow_tasks.query_by_flow_run("run-r")]
    assert task_id("run-r", "three") not in ids, "three waits for two to finish"
    await world.execute_ready("run-r")
    await restarted.advance("run-r")
    await world.execute_ready("run-r")
    await restarted.advance("run-r")
    assert world.side_effects["run-r:one:0:1"] == 1, "a done node is never re-run"
    assert world.side_effects["run-r:two:0:2"] == 1
    await assert_exactly_once(world, "run-r")


async def test_node_states_reconciled_from_rows_on_resume() -> None:
    backend = FakeBackend()
    await backend.start()
    world = World(backend)
    runner = world.new_runner()
    await runner.start_run(
        "chain3", input={}, initiator="operator", initiator_did="did:arc:x/1", run_id="run-n"
    )
    # The node finishes while no runner is alive to observe it.
    await world.execute_ready("run-n")
    stale = await world.runs.get("run-n")
    assert stale is not None and stale.node_states["one"].status == "materialized"

    await world.new_runner().resume()

    fresh = await world.runs.get("run-n")
    assert fresh is not None and fresh.node_states["one"].status == "done"


async def test_reclaim_changes_attempt_key() -> None:
    backend = FakeBackend()
    await backend.start()
    world = World(backend)
    runner = world.new_runner()
    await runner.start_run(
        "chain3", input={}, initiator="operator", initiator_did="did:arc:x/1", run_id="run-k"
    )
    one = task_id("run-k", "one")
    row = await world.tasks.get(one)
    assert row is not None
    first_key = attempt_key(row, row.attempts + 1)
    claimed, _ = await world.tasks.start_task(one, SALES_DID, attempt_key=first_key)
    assert claimed is not None and claimed.metadata["attempt_key"] == first_key

    await world.new_runner().resume()

    row = await world.tasks.get(one)
    assert row is not None and row.status == "todo"
    second_key = attempt_key(row, row.attempts + 1)
    assert second_key != first_key
    # The stale attempt can no longer record a result: its attempt is gone.
    assert (
        await world.tasks.complete_attempt(
            one,
            attempt_key=first_key,
            attempts=claimed.attempts,
            resolution="late",
            output={},
            actor_did=SALES_DID,
        )
        is None
    )
    reclaimed, _ = await world.tasks.start_task(one, SALES_DID, attempt_key=second_key)
    assert reclaimed is not None and reclaimed.metadata["attempt_key"] == second_key


async def test_double_fire_same_occurrence_creates_one_run() -> None:
    backend = FakeBackend()
    await backend.start()
    world = World(backend)
    runner = world.new_runner()
    for _ in range(2):
        await runner.start_run(
            "chain3",
            input={},
            initiator="scheduler",
            initiator_did="did:arc:x/scheduler",
            run_id="run-occ",
            detached=True,
        )
    await runner.tick()
    await runner.tick()
    assert len(await world.runs.list_for_workflow("chain3")) == 1
    assert [r.id for r in await world.flow_tasks.query_by_flow_run("run-occ")] == [
        task_id("run-occ", "one")
    ]


async def test_resume_without_the_lease_waits_for_the_next_tick() -> None:
    """A standby process resumes nothing until it holds the lease; then it does."""
    backend = FakeBackend()
    await backend.start()
    world = World(backend)
    runner = world.new_runner()
    await runner.start_run(
        "chain3", input={}, initiator="operator", initiator_did="did:arc:x/1", run_id="run-l"
    )
    await world.execute_ready("run-l")

    class Lease:
        held = False
        fence = None

        async def acquire_or_renew(self) -> Any:
            return object() if self.held else None

        async def release(self) -> None:
            return None

    sleeps: list[float] = []

    async def no_sleep(delay: float) -> None:
        sleeps.append(delay)

    lease = Lease()
    standby = world.new_runner()
    standby._lease = lease  # type: ignore[assignment]
    standby._sleep = no_sleep  # type: ignore[method-assign]
    assert await standby.resume() == 0
    assert (await world.runs.get("run-l")).node_states["one"].status == "materialized"  # type: ignore[union-attr]
    lease.held = True
    await standby.tick()
    assert (await world.runs.get("run-l")).node_states["one"].status == "done"  # type: ignore[union-attr]


async def test_reclaim_stamps_reclaimed_at_on_the_row() -> None:
    backend = FakeBackend()
    await backend.start()
    world = World(backend)
    await world.new_runner().start_run(
        "chain3", input={}, initiator="operator", initiator_did="did:arc:x/1", run_id="run-ra"
    )
    one = task_id("run-ra", "one")
    row = await world.tasks.get(one)
    assert row is not None
    await world.tasks.start_task(one, SALES_DID, attempt_key=attempt_key(row, 1))
    before = await world.tasks.get(one)
    assert before is not None and "reclaimed_at" not in before.metadata

    await world.new_runner().resume()

    reclaimed = await world.tasks.get(one)
    assert reclaimed is not None and reclaimed.status == "todo"
    assert datetime.fromisoformat(str(reclaimed.metadata["reclaimed_at"])).tzinfo is not None
    assert reclaimed.metadata["flow_run_id"] == "run-ra", "the node block survives the stamp"


def test_attempt_with_its_own_timeout_is_not_expired_inside_the_margin() -> None:
    """A slow turn finishing at its timeout must never be reclaimed and double-run."""
    from arcstore.tasks import RECLAIM_MARGIN_S

    from arcteam.workflow.stores import _attempt_expired

    now = datetime.now(UTC)
    row = Task(id="t", title="t", status="in_progress", creator_did=OPS_DID, timeout_seconds=1200)
    row = row.model_copy(update={"started_at": (now - timedelta(seconds=1200)).isoformat()})
    assert not _attempt_expired(row, now, 900.0)
    past = now + timedelta(seconds=RECLAIM_MARGIN_S + 1)
    assert _attempt_expired(row, past, 900.0)
