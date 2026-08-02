"""arcstore ``TaskStore.create_batch`` (SPEC-061 ArcFlow COMP-007) — RED.

Batch creation of task rows across DIFFERENT owners in one backend
transaction, idempotent on each task's own id so a crashed caller re-invoking
the same batch gets the existing rows back rather than duplicating them
(T-837/REQ-229/REQ-234). Mirrors the workflow runner's frontier-materialization
contract: a batch spans several owning agents and must commit atomically.

``TaskStore.create_batch`` does not exist yet. Every import is local to its
test so a missing method surfaces as one failure per test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from arcstore.backends.sqlite import SqliteBackend

_CREATOR = "did:arc:test:exec/runner00"
_AGENT_A = "did:arc:test:exec/aaaaaaaa"
_AGENT_B = "did:arc:test:exec/bbbbbbbb"
_AGENT_C = "did:arc:test:exec/cccccccc"
_RUN_ID = "run-frontier-01"


async def _backend(tmp_path: Path) -> SqliteBackend:
    be = SqliteBackend(tmp_path / "store.db")
    await be.start()
    return be


def _node_task(node_id: str, owner_did: str) -> Any:
    from arcstore.tasks import Task

    # The runner's convention (arcteam, out of arcstore's scope): the task id
    # deterministically encodes the (run_id, node_id) identity pair, which is
    # what makes create_batch's idempotency meaningful across a replay.
    return Task(
        id=f"{_RUN_ID}:{node_id}",
        title=f"Run node {node_id}",
        creator_did=_CREATOR,
        owner_did=owner_did,
        run_id=_RUN_ID,
        metadata={"node_id": node_id, "workflow_id": "wf-onboarding"},
    )


class TestCreateBatchAtomicity:
    async def test_batch_spanning_several_owners_commits_all_rows(self, tmp_path: Path) -> None:
        from arcstore.tasks import TaskStore

        be = await _backend(tmp_path)
        try:
            store = TaskStore(be)
            batch = [
                _node_task("n1", _AGENT_A),
                _node_task("n2", _AGENT_B),
                _node_task("n3", _AGENT_C),
            ]
            created = await store.create_batch(batch, actor_did=_CREATOR)
            assert len(created) == 3
            assert {t.owner_did for t in created} == {_AGENT_A, _AGENT_B, _AGENT_C}

            for node_id, owner in (("n1", _AGENT_A), ("n2", _AGENT_B), ("n3", _AGENT_C)):
                row = await store.get(f"{_RUN_ID}:{node_id}")
                assert row is not None
                assert row.owner_did == owner
                assert row.status == "todo"  # owned tasks default to todo (create() parity)
        finally:
            await be.stop()

    async def test_batch_preserves_input_order_in_returned_rows(self, tmp_path: Path) -> None:
        from arcstore.tasks import TaskStore

        be = await _backend(tmp_path)
        try:
            store = TaskStore(be)
            batch = [
                _node_task("n3", _AGENT_C),
                _node_task("n1", _AGENT_A),
                _node_task("n2", _AGENT_B),
            ]
            created = await store.create_batch(batch, actor_did=_CREATOR)
            assert [t.metadata["node_id"] for t in created] == ["n3", "n1", "n2"]
        finally:
            await be.stop()

    async def test_replaying_the_same_batch_returns_existing_rows_not_duplicates(
        self, tmp_path: Path
    ) -> None:
        """Simulated crash: the caller re-invokes create_batch with the exact
        same (run_id, node_id)-keyed tasks after a partial or full prior
        attempt. It must get the existing rows back, not create new ones or
        overwrite the first attempt's state."""
        from arcstore.tasks import TaskStore

        be = await _backend(tmp_path)
        try:
            store = TaskStore(be)
            batch = [_node_task("n1", _AGENT_A), _node_task("n2", _AGENT_B)]

            first = await store.create_batch(batch, actor_did=_CREATOR)
            first_created_at = {t.id: t.created_at for t in first}

            # The agent owning n1 claims and starts its task between the two
            # create_batch calls — a real crash-and-replay would see this.
            await store.start_task(f"{_RUN_ID}:n1", _AGENT_A)

            replay = await store.create_batch(batch, actor_did=_CREATOR)
            assert len(replay) == 2

            replayed_n1 = next(t for t in replay if t.metadata["node_id"] == "n1")
            replayed_n2 = next(t for t in replay if t.metadata["node_id"] == "n2")

            # n1's in-progress claim survives — the replay must not clobber it
            # back to its pre-claim state.
            assert replayed_n1.status == "in_progress"
            assert replayed_n1.owner_did == _AGENT_A
            # created_at is unchanged — proves this is the ORIGINAL row, not a
            # freshly re-inserted duplicate.
            assert replayed_n1.created_at == first_created_at[replayed_n1.id]
            assert replayed_n2.created_at == first_created_at[replayed_n2.id]

            all_tasks = await store.list()
            matching = [t for t in all_tasks if t.run_id == _RUN_ID]
            assert len(matching) == 2, "replay must not create duplicate rows"
        finally:
            await be.stop()

    async def test_batch_emits_one_audit_event_for_the_whole_batch(self, tmp_path: Path) -> None:
        from arcstore.tasks import TaskStore

        class _RecordingSink:
            def __init__(self) -> None:
                self.events: list[Any] = []

            def write(self, event: Any) -> None:
                self.events.append(event)

        be = await _backend(tmp_path)
        sink = _RecordingSink()
        try:
            store = TaskStore(be, sink=sink)
            batch = [_node_task("n1", _AGENT_A), _node_task("n2", _AGENT_B)]
            await store.create_batch(batch, actor_did=_CREATOR)
            assert len(sink.events) == 1
            assert sink.events[0].actor_did == _CREATOR
        finally:
            await be.stop()
