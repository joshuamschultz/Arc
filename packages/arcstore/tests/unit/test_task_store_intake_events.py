"""SPEC-088 T-1355 (RED): TaskStore emits intake terminal work events (COMP-002, COMP-012).

Injection (the smallest change consistent with SDD COMP-002): ``TaskStore``
takes an optional keyword ``work_events: WorkEventLog | None = None``::

    TaskStore(backend, sink=..., work_events=WorkEventLog(backend))

With no log the store behaves exactly as today. With a log, every terminal
transition of a row whose ``metadata`` carries ``intake`` appends one work
event in the same call path:

* ``finish(status="done")`` and ``complete_attempt`` -> ``run.finished``;
* ``finish(status="failed")``, ``fail_attempt`` and ``dead_letter`` ->
  ``run.failed``.

The event carries ``event_id`` = a deterministic sha256 hex digest over
``("intake", task_id, final_status)``, ``run_id = metadata.intake.run_id``,
``correlation_id`` and ``callback_url`` from ``metadata.intake``,
``agent_did = owner_did``, ``task_id``, ``status`` = the final task status and
``classification`` = the task's classification. A non-intake row appends
nothing; a repeated terminal write appends no duplicate (the deterministic id
plus the log's insert-if-absent).

Runs on the memory backend and on PostgreSQL (skipped without
``ARCSTORE_TEST_DATABASE_URL``). Ids are unique per test and every read is
filtered by ``task_id``, so a shared database never leaks across runs.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import pytest
from pydantic import SecretStr

from arcstore.backends.base import ArcStoreBackend
from arcstore.backends.memory import FakeBackend
from arcstore.backends.postgres import PostgresBackend
from arcstore.config import ArcStoreConfig
from arcstore.tasks import Task, TaskStore
from arcstore.work_events import WorkEvent, WorkEventLog

_AGENT = "did:arc:test:intake-events/aaaaaaaa"
_APP = "did:arc:test:intake-events/app00000"
_CALLBACK = "https://hooks.example.test/arc"
_CORRELATION = "corr-PO-7731"
_ID_PREFIX = "t1355-"
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


@pytest.fixture(params=["memory", "postgres"])
async def backend(request: pytest.FixtureRequest) -> AsyncIterator[ArcStoreBackend]:
    """The memory backend, and a PostgreSQL twin that skips without a test DSN."""
    if request.param == "postgres":
        dsn = os.environ.get("ARCSTORE_TEST_DATABASE_URL")
        if not dsn:
            pytest.skip("ARCSTORE_TEST_DATABASE_URL is required for PostgreSQL integration tests")
        real: ArcStoreBackend = PostgresBackend(ArcStoreConfig().postgres_settings(SecretStr(dsn)))
    else:
        real = FakeBackend()
    await real.start()
    try:
        yield real
    finally:
        try:
            for row in await real.mutable_query("tasks"):
                task_id = str(row.get("id", ""))
                if task_id.startswith(_ID_PREFIX):
                    await real.mutable_delete("tasks", task_id, actor_did=_APP)
        finally:
            await real.stop()


def _stores(backend: ArcStoreBackend) -> tuple[TaskStore, WorkEventLog]:
    log = WorkEventLog(backend)
    return TaskStore(backend, work_events=log), log


def _intake_task(*, classification: str = "CUI", callback: str | None = _CALLBACK) -> Task:
    run_id = str(uuid.uuid4())
    return Task(
        id=f"{_ID_PREFIX}intake-{run_id}",
        title="Summarize open purchase orders",
        description="Summarize open purchase orders",
        status="todo",
        owner_did=_AGENT,
        creator_did=_APP,
        max_attempts=1,
        tags=["intake"],
        classification=classification,
        metadata={
            "intake": {
                "run_id": run_id,
                "run_purpose": "manual",
                "correlation_id": _CORRELATION,
                "callback_url": callback,
                "on_behalf_of": None,
                "via_app": None,
            },
            "app": {"customer_ref": "ACME"},
        },
    )


async def _started(store: TaskStore, task: Task, *, attempt_key: str = "k-1") -> Task:
    await store.create(task)
    started, reason = await store.start_task(
        task.id, _AGENT, run_id=task.metadata["intake"]["run_id"], attempt_key=attempt_key
    )
    assert reason == "assigned" and started is not None
    return started


async def _events(log: WorkEventLog, task_id: str) -> list[WorkEvent]:
    return await log.read(0, limit=100, where={"task_id": task_id})


def _assert_envelope(event: WorkEvent, task: Task, *, kind: str, status: str) -> None:
    intake: dict[str, Any] = task.metadata["intake"]
    assert event.kind == kind
    assert event.status == status
    assert event.task_id == task.id
    assert event.run_id == intake["run_id"]
    assert event.correlation_id == _CORRELATION
    assert event.callback_url == intake["callback_url"]
    assert event.agent_did == _AGENT
    assert event.classification == task.classification
    assert event.workflow_id is None and event.node_id is None
    assert _SHA256_HEX.match(event.event_id), event.event_id
    occurred = datetime.fromisoformat(event.occurred_at)
    assert occurred.tzinfo is not None


async def test_finish_done_on_intake_row_appends_run_finished(backend: ArcStoreBackend) -> None:
    store, log = _stores(backend)
    task = await _started(store, _intake_task())

    await store.finish(
        task.id, status="done", resolution="ok", actor_did=_AGENT, output={"text": "done"}
    )

    events = await _events(log, task.id)
    assert len(events) == 1
    _assert_envelope(events[0], task, kind="run.finished", status="done")


async def test_finish_failed_on_intake_row_appends_run_failed(backend: ArcStoreBackend) -> None:
    store, log = _stores(backend)
    task = await _started(store, _intake_task())

    await store.finish(
        task.id, status="failed", resolution="loop halted", actor_did=_AGENT, last_error="max_cost"
    )

    events = await _events(log, task.id)
    assert len(events) == 1
    _assert_envelope(events[0], task, kind="run.failed", status="failed")


async def test_complete_attempt_on_intake_row_appends_run_finished(
    backend: ArcStoreBackend,
) -> None:
    store, log = _stores(backend)
    task = await _started(store, _intake_task(), attempt_key="k-1")

    done = await store.complete_attempt(
        task.id,
        attempt_key="k-1",
        attempts=task.attempts,
        resolution="ok",
        output={"text": "answer"},
        actor_did=_AGENT,
    )
    assert done is not None and done.status == "done"

    events = await _events(log, task.id)
    assert len(events) == 1
    _assert_envelope(events[0], task, kind="run.finished", status="done")


async def test_final_fail_attempt_on_intake_row_appends_run_failed(
    backend: ArcStoreBackend,
) -> None:
    store, log = _stores(backend)
    task = await _started(store, _intake_task(), attempt_key="k-1")

    failed = await store.fail_attempt(
        task.id,
        attempt_key="k-1",
        attempts=task.attempts,
        resolution="cannot",
        actor_did=_AGENT,
        last_error="tool denied",
    )
    assert failed is not None and failed.status == "failed"

    events = await _events(log, task.id)
    assert len(events) == 1
    _assert_envelope(events[0], task, kind="run.failed", status="failed")


async def test_dead_letter_on_intake_row_appends_run_failed(backend: ArcStoreBackend) -> None:
    store, log = _stores(backend)
    task = await _started(store, _intake_task())

    dead = await store.dead_letter(
        task.id, actor_did=_AGENT, resolution="exhausted", last_error="RuntimeError: boom"
    )
    assert dead is not None and dead.status == "failed"

    events = await _events(log, task.id)
    assert len(events) == 1
    _assert_envelope(events[0], task, kind="run.failed", status="failed")


async def test_intake_row_without_callback_still_appends_event(backend: ArcStoreBackend) -> None:
    """The SSE stream reads the log too; no callback must not mean no event."""
    store, log = _stores(backend)
    task = await _started(store, _intake_task(callback=None))

    await store.finish(task.id, status="done", resolution="ok", actor_did=_AGENT)

    events = await _events(log, task.id)
    assert len(events) == 1
    assert events[0].callback_url is None
    assert events[0].kind == "run.finished"


async def test_non_intake_row_appends_no_event(backend: ArcStoreBackend) -> None:
    store, log = _stores(backend)
    plain = Task(
        id=f"{_ID_PREFIX}plain-{uuid.uuid4().hex}",
        title="Board task",
        status="todo",
        owner_did=_AGENT,
        creator_did=_APP,
        # Caller metadata that merely looks like intake data is not intake.
        metadata={"app": {"intake": {"run_id": "spoof", "callback_url": _CALLBACK}}},
    )
    await store.create(plain)
    await store.start_task(plain.id, _AGENT)

    await store.finish(plain.id, status="done", resolution="ok", actor_did=_AGENT)

    assert await _events(log, plain.id) == []


async def test_repeated_finish_appends_no_duplicate(backend: ArcStoreBackend) -> None:
    store, log = _stores(backend)
    task = await _started(store, _intake_task())

    await store.finish(task.id, status="done", resolution="ok", actor_did=_AGENT)
    await store.finish(task.id, status="done", resolution="ok again", actor_did=_AGENT)

    events = await _events(log, task.id)
    assert len(events) == 1


async def test_stale_attempt_writes_after_terminal_append_nothing(
    backend: ArcStoreBackend,
) -> None:
    """A refused (stale) terminal write must not emit: the event follows the write."""
    store, log = _stores(backend)
    task = await _started(store, _intake_task(), attempt_key="k-1")
    await store.dead_letter(task.id, actor_did=_AGENT, resolution="cancelled", last_error="x")

    late = await store.complete_attempt(
        task.id,
        attempt_key="k-1",
        attempts=task.attempts,
        resolution="late",
        output={"text": "late"},
        actor_did=_AGENT,
    )
    assert late is None
    again = await store.dead_letter(task.id, actor_did=_AGENT, resolution="again", last_error="y")
    assert again is None

    events = await _events(log, task.id)
    assert [e.kind for e in events] == ["run.failed"]


async def test_event_id_is_deterministic_per_task_and_final_status(
    backend: ArcStoreBackend,
) -> None:
    """Same (task, status) -> same id on a fresh store; a different status -> a new id."""
    store, log = _stores(backend)
    task = await _started(store, _intake_task())
    await store.finish(task.id, status="failed", resolution="first", actor_did=_AGENT)
    (first,) = await _events(log, task.id)

    # A second store instance over the same backend must derive the same id,
    # so the log's insert-if-absent drops the replay.
    replay_store, _ = _stores(backend)
    await replay_store.finish(task.id, status="failed", resolution="replay", actor_did=_AGENT)
    assert [e.event_id for e in await _events(log, task.id)] == [first.event_id]

    # An operator re-finishing the row as done is a different final status.
    await store.finish(task.id, status="done", resolution="override", actor_did=_AGENT)
    events = await _events(log, task.id)
    assert len(events) == 2
    assert events[1].kind == "run.finished"
    assert events[1].event_id != first.event_id


async def test_event_ids_differ_across_tasks(backend: ArcStoreBackend) -> None:
    store, log = _stores(backend)
    one = await _started(store, _intake_task())
    await store.finish(one.id, status="done", resolution="ok", actor_did=_AGENT)
    # One in_progress per owner: start the second after the first is terminal.
    two = await _started(store, _intake_task())
    await store.finish(two.id, status="done", resolution="ok", actor_did=_AGENT)

    (e1,) = await _events(log, one.id)
    (e2,) = await _events(log, two.id)
    assert e1.event_id != e2.event_id


async def test_store_without_work_events_still_finishes_intake_row(
    backend: ArcStoreBackend,
) -> None:
    """Guard: the log is optional; an unwired store keeps today's behavior."""
    store = TaskStore(backend)
    log = WorkEventLog(backend)
    task = await _started(store, _intake_task())

    done = await store.finish(task.id, status="done", resolution="ok", actor_did=_AGENT)

    assert done is not None and done.status == "done"
    assert await _events(log, task.id) == []
