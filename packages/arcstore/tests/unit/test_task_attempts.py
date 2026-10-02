"""Attempt identity on the task row (P14-B step 2): claim, complete, reclaim.

An attempt number names exactly one claim, and only the executor holding that
claim may record its result. The races here are forced: a hook runs the
competing writer INSIDE the window between the store's read and its
conditional write, so the guard under test is the only thing that can stop it
([[feedback_concurrency_tests_must_interleave]]).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from arcstore.backends.memory import FakeBackend
from arcstore.tasks import Task, TaskStore

_AGENT = "did:arc:test:agent/aaaa1111"
_OTHER = "did:arc:test:agent/bbbb2222"


class _HookedBackend(FakeBackend):
    """Runs ``hook`` once, just before the next conditional write on ``tasks``."""

    def __init__(self) -> None:
        super().__init__()
        self.hook: Callable[[], Awaitable[None]] | None = None

    async def update_if(self, collection: str, key: str, *args: Any, **kwargs: Any) -> bool:
        if self.hook is not None and collection == "tasks":
            hook, self.hook = self.hook, None
            await hook()
        return await super().update_if(collection, key, *args, **kwargs)


async def _store() -> tuple[_HookedBackend, TaskStore]:
    backend = _HookedBackend()
    await backend.start()
    store = TaskStore(backend)
    await store.create(
        Task(
            id="t1",
            title="node",
            status="todo",
            owner_did=_AGENT,
            creator_did=_AGENT,
            metadata={"node_id": "a"},
        )
    )
    return backend, store


async def test_claim_stamps_the_attempt_key_and_keeps_the_metadata() -> None:
    _, store = await _store()
    claimed, reason = await store.start_task("t1", _AGENT, attempt_key="r:a:0:1")
    assert reason == "assigned"
    assert claimed is not None
    assert claimed.metadata == {
        "node_id": "a",
        "attempt_key": "r:a:0:1",
        "attempt_started_at": claimed.started_at,
    }


async def test_a_claim_cannot_land_an_attempt_number_already_used() -> None:
    backend, store = await _store()

    async def another_claim_and_requeue() -> None:
        await TaskStore(backend).start_task("t1", _AGENT, attempt_key="r:a:0:1")
        await TaskStore(backend).requeue(
            "t1", actor_did=_OTHER, last_error="died", next_attempt_at="2000-01-01T00:00:00+00:00"
        )

    backend.hook = another_claim_and_requeue
    claimed, reason = await store.start_task("t1", _AGENT, attempt_key="r:a:0:1")
    assert (claimed, reason) == (None, "no_tasks_available")
    row = await store.get("t1")
    assert row is not None and row.status == "todo" and row.attempts == 1


async def test_complete_attempt_loses_to_a_reclaim_inside_its_write_window() -> None:
    backend, store = await _store()
    claimed, _ = await store.start_task("t1", _AGENT)  # an executor that stamped no key
    assert claimed is not None

    async def reclaim_and_reclaim_again() -> None:
        other = TaskStore(backend)
        await other.requeue(
            "t1", actor_did=_OTHER, last_error="stale", next_attempt_at="2000-01-01T00:00:00+00:00"
        )
        await other.start_task("t1", _AGENT)

    backend.hook = reclaim_and_reclaim_again
    result = await store.complete_attempt(
        "t1",
        attempt_key="r:a:0:1",
        attempts=claimed.attempts,
        resolution="late",
        output={"x": 1},
        actor_did=_AGENT,
    )
    assert result is None
    row = await store.get("t1")
    assert row is not None and row.status == "in_progress" and row.attempts == 2
    assert row.output is None


async def test_forged_attempt_key_is_rejected_even_with_the_right_attempt_number() -> None:
    _, store = await _store()
    claimed, _ = await store.start_task("t1", _AGENT, attempt_key="r:a:0:1")
    assert claimed is not None
    forged = await store.complete_attempt(
        "t1",
        attempt_key="r:a:0:999",
        attempts=claimed.attempts,
        resolution="forged",
        output={"x": 1},
        actor_did=_AGENT,
    )
    assert forged is None
    genuine = await store.complete_attempt(
        "t1",
        attempt_key="r:a:0:1",
        attempts=claimed.attempts,
        resolution="ok",
        output={"x": 1},
        actor_did=_AGENT,
    )
    assert genuine is not None and genuine.metadata["attempt_result_key"] == "r:a:0:1"


async def test_requeue_pinned_to_an_attempt_leaves_a_newer_attempt_alone() -> None:
    _, store = await _store()
    first, _ = await store.start_task("t1", _AGENT)
    assert first is not None
    await store.requeue(
        "t1", actor_did=_OTHER, last_error="x", next_attempt_at="2000-01-01T00:00:00+00:00"
    )
    await store.start_task("t1", _AGENT)
    stale = await store.requeue(
        "t1",
        actor_did=_OTHER,
        last_error="stale reclaimer",
        next_attempt_at="2000-01-01T00:00:00+00:00",
        expected_attempts=first.attempts,
    )
    assert stale is None
    row = await store.get("t1")
    assert row is not None and row.status == "in_progress" and row.attempts == 2
