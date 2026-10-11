"""SPEC-088 T-1333 (RED): ``TaskStore.create_if_absent`` and ``TaskStore.validate_links``.

COMP-002 (SDD):

* ``create_if_absent(task, *, request_digest) -> tuple[Task, bool]`` creates the
  row only when its id is free. The digest is stored at
  ``metadata.request_digest``. The same id with the same digest returns
  ``(existing, False)``; a different digest raises ``TaskConflictError``. It is
  one backend write, so two concurrent calls can never both report created.
* ``validate_links(parent_id, blocked_by)`` refuses a missing parent, a missing
  blocker, more than 50 blockers and any cycle reached by a bounded walk, with
  ``TaskLinkError``.

Every test runs on the memory backend and on PostgreSQL. The PostgreSQL twin
uses the shared ``postgres_backend`` fixture, which skips unless
``ARCSTORE_TEST_DATABASE_URL`` is set. Ids are unique per test so a shared
database never leaks state across runs.

The concurrency test follows [[feedback_concurrency_tests_must_interleave]]: a
barrier holds both callers at their first backend call so a read-then-write
implementation is forced to race, not run one after the other.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import SecretStr

from arcstore.backends.base import ArcStoreBackend
from arcstore.backends.memory import FakeBackend
from arcstore.backends.postgres import PostgresBackend
from arcstore.config import ArcStoreConfig

_CREATOR = "did:arc:test:intake/creator0"
_AGENT = "did:arc:test:intake/aaaaaaaa"
_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64
# Every task id this module creates starts with this, so the PostgreSQL twin can
# delete exactly its own rows at teardown and leave the shared board clean.
_ID_PREFIX = "t1333-"


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
            await _delete_own_rows(real)
        finally:
            await real.stop()


async def _delete_own_rows(backend: ArcStoreBackend) -> None:
    """Remove the task rows this module created; other suites share the database."""
    rows = await backend.mutable_query("tasks")
    for row in rows:
        task_id = str(row.get("id", ""))
        if task_id.startswith(_ID_PREFIX):
            await backend.mutable_delete("tasks", task_id, actor_did=_CREATOR)


def _id(label: str = "intake") -> str:
    return f"{_ID_PREFIX}{label}-{uuid.uuid4().hex}"


def _task(task_id: str, **fields: Any) -> Any:
    from arcstore.tasks import Task

    payload: dict[str, Any] = {
        "id": task_id,
        "title": "Summarize the order",
        "creator_did": _CREATOR,
        "owner_did": _AGENT,
    }
    payload.update(fields)
    return Task.model_validate(payload)


def _store(backend: Any) -> Any:
    from arcstore.tasks import TaskStore

    return TaskStore(backend)


def _errors() -> tuple[type[Exception], type[Exception]]:
    import arcstore.tasks as tasks

    return tasks.TaskConflictError, tasks.TaskLinkError


class TestCreateIfAbsent:
    """REQ-593: idempotent intake row creation keyed by id and request digest."""

    async def test_create_if_absent_new_id_creates_and_stores_digest(
        self, backend: ArcStoreBackend
    ) -> None:
        store = _store(backend)
        task_id = _id()

        created, was_created = await store.create_if_absent(
            _task(task_id), request_digest=_DIGEST_A
        )

        assert was_created is True
        assert created.id == task_id
        assert created.metadata["request_digest"] == _DIGEST_A
        stored = await store.get(task_id)
        assert stored is not None
        assert stored.metadata["request_digest"] == _DIGEST_A
        assert stored.created_at is not None

    async def test_create_if_absent_same_id_same_digest_returns_existing_not_created(
        self, backend: ArcStoreBackend
    ) -> None:
        store = _store(backend)
        task_id = _id()
        first, _ = await store.create_if_absent(
            _task(task_id, title="Original title"), request_digest=_DIGEST_A
        )

        # A replay carries the same digest; whatever else it holds, the stored
        # row is what comes back and it is not rewritten.
        replay, was_created = await store.create_if_absent(
            _task(task_id, title="Replayed title"), request_digest=_DIGEST_A
        )

        assert was_created is False
        assert replay.title == "Original title"
        assert replay.created_at == first.created_at
        stored = await store.get(task_id)
        assert stored is not None
        assert stored.title == "Original title"

    async def test_create_if_absent_same_id_different_digest_raises_conflict(
        self, backend: ArcStoreBackend
    ) -> None:
        conflict_error, _ = _errors()
        store = _store(backend)
        task_id = _id()
        await store.create_if_absent(_task(task_id, title="First"), request_digest=_DIGEST_A)

        with pytest.raises(conflict_error):
            await store.create_if_absent(_task(task_id, title="Second"), request_digest=_DIGEST_B)

        stored = await store.get(task_id)
        assert stored is not None
        assert stored.title == "First"
        assert stored.metadata["request_digest"] == _DIGEST_A

    async def test_create_if_absent_over_row_without_digest_raises_conflict(
        self, backend: ArcStoreBackend
    ) -> None:
        # Adversarial: a caller who guesses an existing internal task id must
        # not get that row back as a "replay". No stored digest != any digest.
        conflict_error, _ = _errors()
        store = _store(backend)
        task_id = _id()
        await store.create(_task(task_id, title="Internal task"))

        with pytest.raises(conflict_error):
            await store.create_if_absent(_task(task_id), request_digest=_DIGEST_A)

        stored = await store.get(task_id)
        assert stored is not None
        assert stored.title == "Internal task"
        assert "request_digest" not in stored.metadata

    async def test_create_if_absent_digest_argument_wins_over_caller_metadata(
        self, backend: ArcStoreBackend
    ) -> None:
        # Adversarial: a forged metadata.request_digest must not become the key.
        store = _store(backend)
        task_id = _id()
        forged = _task(task_id, metadata={"request_digest": _DIGEST_B, "app": {"k": "v"}})

        created, was_created = await store.create_if_absent(forged, request_digest=_DIGEST_A)

        assert was_created is True
        assert created.metadata["request_digest"] == _DIGEST_A
        assert created.metadata["app"] == {"k": "v"}

    async def test_create_if_absent_concurrent_same_id_creates_exactly_once(
        self, backend: ArcStoreBackend
    ) -> None:
        store = _store(_FirstCallBarrier(backend, parties=2))
        task_id = _id()

        results = await asyncio.wait_for(
            asyncio.gather(
                store.create_if_absent(_task(task_id), request_digest=_DIGEST_A),
                store.create_if_absent(_task(task_id), request_digest=_DIGEST_A),
            ),
            timeout=10,
        )

        flags = sorted(was_created for _, was_created in results)
        assert flags == [False, True]
        (first, _), (second, _) = results
        assert first.id == second.id == task_id
        assert first.created_at == second.created_at


class TestValidateLinks:
    """REQ-601: parent and blocker links must exist, stay bounded and be acyclic."""

    async def test_validate_links_no_links_is_accepted(self, backend: ArcStoreBackend) -> None:
        await _store(backend).validate_links(None, [])

    async def test_validate_links_existing_parent_and_blockers_is_accepted(
        self, backend: ArcStoreBackend
    ) -> None:
        store = _store(backend)
        parent, blocker_one, blocker_two = _id("parent"), _id("blk"), _id("blk")
        await store.create_batch(
            [_task(parent), _task(blocker_one), _task(blocker_two)], actor_did=_CREATOR
        )

        await store.validate_links(parent, [blocker_one, blocker_two])

    async def test_validate_links_missing_parent_raises(self, backend: ArcStoreBackend) -> None:
        _, link_error = _errors()
        with pytest.raises(link_error):
            await _store(backend).validate_links(_id("ghost-parent"), [])

    async def test_validate_links_missing_blocker_raises(self, backend: ArcStoreBackend) -> None:
        _, link_error = _errors()
        store = _store(backend)
        real = _id("blk")
        await store.create(_task(real))

        with pytest.raises(link_error):
            await store.validate_links(None, [real, _id("ghost-blocker")])

    async def test_validate_links_fifty_blockers_is_accepted(
        self, backend: ArcStoreBackend
    ) -> None:
        store = _store(backend)
        ids = [_id("blk") for _ in range(50)]
        await store.create_batch([_task(i) for i in ids], actor_did=_CREATOR)

        await store.validate_links(None, ids)

    async def test_validate_links_fifty_one_blockers_raises(
        self, backend: ArcStoreBackend
    ) -> None:
        _, link_error = _errors()
        store = _store(backend)
        ids = [_id("blk") for _ in range(51)]
        await store.create_batch([_task(i) for i in ids], actor_did=_CREATOR)

        with pytest.raises(link_error):
            await store.validate_links(None, ids)

    async def test_validate_links_two_node_blocker_cycle_raises(
        self, backend: ArcStoreBackend
    ) -> None:
        _, link_error = _errors()
        store = _store(backend)
        a, b = _id("cyc"), _id("cyc")
        await store.create_batch(
            [_task(a, blocked_by=[b]), _task(b, blocked_by=[a])], actor_did=_CREATOR
        )

        with pytest.raises(link_error):
            await store.validate_links(None, [a])

    async def test_validate_links_two_node_parent_cycle_raises(
        self, backend: ArcStoreBackend
    ) -> None:
        _, link_error = _errors()
        store = _store(backend)
        a, b = _id("cyc"), _id("cyc")
        await store.create_batch(
            [_task(a, parent_id=b), _task(b, parent_id=a)], actor_did=_CREATOR
        )

        with pytest.raises(link_error):
            await store.validate_links(a, [])

    async def test_validate_links_acyclic_chain_is_accepted(
        self, backend: ArcStoreBackend
    ) -> None:
        # A diamond (two paths to one root) is not a cycle.
        store = _store(backend)
        root, left, right = _id("root"), _id("left"), _id("right")
        await store.create_batch(
            [_task(root), _task(left, blocked_by=[root]), _task(right, blocked_by=[root])],
            actor_did=_CREATOR,
        )

        await store.validate_links(None, [left, right])

    async def test_validate_links_chain_deeper_than_walk_bound_raises(
        self, backend: ArcStoreBackend
    ) -> None:
        # Beyond PLAN: SDD bounds the walk at depth 64. A chain the walk cannot
        # finish must fail closed, not be accepted as "no cycle found".
        _, link_error = _errors()
        store = _store(backend)
        chain = [_id("deep") for _ in range(70)]
        rows = [_task(chain[0])] + [
            _task(chain[i], blocked_by=[chain[i - 1]]) for i in range(1, len(chain))
        ]
        await store.create_batch(rows, actor_did=_CREATOR)

        with pytest.raises(link_error):
            await store.validate_links(None, [chain[-1]])


class _FirstCallBarrier:
    """Backend proxy that holds each caller task at its first backend call.

    Both concurrent ``create_if_absent`` calls reach the backend together, so
    an implementation that reads, then writes, sees "absent" twice and the
    test catches the double create. Later calls pass straight through, so an
    implementation whose paths diverge after the first call cannot deadlock.
    """

    def __init__(self, inner: Any, *, parties: int) -> None:
        self._inner = inner
        self._barrier = asyncio.Barrier(parties)
        self._passed: set[asyncio.Task[Any]] = set()

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._inner, name)
        if not inspect.iscoroutinefunction(target):
            return target

        async def gated(*args: Any, **kwargs: Any) -> Any:
            current = asyncio.current_task()
            if current is not None and current not in self._passed:
                self._passed.add(current)
                await asyncio.wait_for(self._barrier.wait(), timeout=5)
            return await target(*args, **kwargs)

        return gated
