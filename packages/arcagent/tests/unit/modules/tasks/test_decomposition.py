"""SPEC-056 Phase 2 — decomposition + dependency DAG (TDD).

Covers the agent-side DAG behavior: a blocked task never dispatches until its
deps are done; ``decompose`` persists parent<->child linkage; a coordinator
parent auto-completes when all children finish and auto-fails when any child
fails terminally; and a cyclic blocked_by is rejected. Drives the real
``_dispatch_tick`` / ``_reliability_tick`` against a real ``TaskStore``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from arctrust import AgentIdentity

_OTHER = "did:arc:test:agent/other0000"


class _Recorder:
    """Records each dispatched run (the task never self-completes)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, text: str, *, session_key: str, run_id: str | None = None) -> str:
        self.calls.append(session_key)
        return "ok"


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    from arcagent.modules.tasks import _runtime

    monkeypatch.delenv("ARCSTORE_DATA_DIR", raising=False)
    _runtime.reset()
    identity = AgentIdentity.generate(org="local", agent_type="agent")
    _runtime.configure(
        config={"enabled": True, "data_dir": str(tmp_path), "dispatch": True},
        telemetry=MagicMock(),
        workspace=tmp_path,
        identity=identity,
    )
    st = _runtime.state()
    st.agent_run_fn = _Recorder()
    yield st, identity
    _runtime.reset()


async def _seed(st: Any, **fields: Any) -> Any:
    from arcstore.tasks import Task

    from arcagent.modules.tasks import _runtime

    await _runtime.ensure_store()
    return await st.store.create(Task(**fields))


@pytest.mark.asyncio
class TestDepsGate:
    async def test_blocked_task_not_dispatched_until_dep_done(self, state: Any) -> None:
        from arcagent.modules.tasks.capabilities import _dispatch_tick

        st, identity = state
        # Dep owned by another agent so this agent's dispatch only considers T.
        await _seed(st, id="dep", title="Dependency", creator_did=_OTHER, owner_did=_OTHER, status="todo")
        await _seed(
            st, id="T", title="Blocked", creator_did=identity.did,
            owner_did=identity.did, status="todo", blocked_by=["dep"],
        )

        await _dispatch_tick()
        assert st.agent_run_fn.calls == []  # dep not done -> T stays blocked

        await st.store.finish("dep", status="done", resolution="ok", actor_did=_OTHER)
        await _dispatch_tick()
        assert any("T" in s for s in st.agent_run_fn.calls)  # now unblocked -> runs


@pytest.mark.asyncio
class TestDecomposeLinkage:
    async def test_decompose_persists_parent_child_links(self, state: Any) -> None:
        from arcagent.modules.tasks.capabilities import create_task, decompose_task

        st, _identity = state
        parent = json.loads(await create_task(title="Big job"))
        result = json.loads(
            await decompose_task(
                id=parent["id"],
                subtasks=[{"title": "Step 1"}, {"title": "Step 2"}],
            )
        )
        child_ids = [c["id"] for c in result["subtasks"]]
        assert len(child_ids) == 2

        stored_parent = await st.store.get(parent["id"])
        assert stored_parent is not None
        assert set(child_ids) <= set(stored_parent.blocked_by)  # parent blocked_by children
        for cid in child_ids:
            child = await st.store.get(cid)
            assert child is not None and child.parent_id == parent["id"]


@pytest.mark.asyncio
class TestParentRollup:
    async def _decompose(self, st: Any) -> tuple[str, list[str]]:
        from arcagent.modules.tasks.capabilities import create_task, decompose_task

        parent = json.loads(await create_task(title="Parent"))
        result = json.loads(
            await decompose_task(
                id=parent["id"], subtasks=[{"title": "A"}, {"title": "B"}]
            )
        )
        return parent["id"], [c["id"] for c in result["subtasks"]]

    async def test_parent_auto_completes_when_all_children_done(self, state: Any) -> None:
        from arcagent.modules.tasks.capabilities import _reliability_tick

        st, identity = state
        parent_id, child_ids = await self._decompose(st)
        for cid in child_ids:
            await st.store.finish(cid, status="done", resolution="done", actor_did=identity.did)

        await _reliability_tick()

        parent = await st.store.get(parent_id)
        assert parent is not None
        assert parent.status == "done"
        assert parent.resolution == "all subtasks complete"

    async def test_parent_auto_fails_when_a_child_fails(self, state: Any) -> None:
        from arcagent.modules.tasks.capabilities import _reliability_tick

        st, identity = state
        parent_id, child_ids = await self._decompose(st)
        await st.store.finish(child_ids[0], status="done", resolution="ok", actor_did=identity.did)
        await st.store.finish(child_ids[1], status="failed", resolution="boom", actor_did=identity.did)

        await _reliability_tick()

        parent = await st.store.get(parent_id)
        assert parent is not None
        assert parent.status == "failed"
        assert "subtask failed" in (parent.resolution or "")

    async def test_coordinator_parent_is_not_dispatched(self, state: Any) -> None:
        from arcagent.modules.tasks.capabilities import _dispatch_tick

        st, identity = state
        parent_id, child_ids = await self._decompose(st)
        # All children done -> parent's deps are met, but it must NOT run (it is a
        # coordinator; it auto-completes instead).
        for cid in child_ids:
            await st.store.finish(cid, status="done", resolution="ok", actor_did=identity.did)

        await _dispatch_tick()

        assert st.agent_run_fn.calls == []  # parent never dispatched
        parent = await st.store.get(parent_id)
        assert parent is not None and parent.status == "todo"  # still open (reconcile completes it)


@pytest.mark.asyncio
class TestCycleRejection:
    async def test_acyclic_blocked_by_is_accepted(self, state: Any) -> None:
        from arcagent.modules.tasks.capabilities import create_task

        st, _identity = state
        dep = json.loads(await create_task(title="Dep"))
        made = json.loads(await create_task(title="Needs dep", blocked_by=[dep["id"]]))
        assert "error" not in made
        assert made["blocked_by"] == [dep["id"]]

    async def test_create_task_rejects_a_cycle(self, state: Any, monkeypatch: Any) -> None:
        # A brand-new task can only self-cycle, and the tool generates the id, so
        # a cycle is unreachable through arguments. Force the store's guard to
        # fire to prove create_task is actually wired to it (defence-in-depth for
        # any future blocked_by-edit path).
        from arcagent.modules.tasks import _runtime
        from arcagent.modules.tasks.capabilities import create_task

        st, _identity = state
        await _runtime.ensure_store()

        async def _always_cycle(_task_id: str, _blocked_by: list[str]) -> bool:
            return True

        monkeypatch.setattr(st.store, "deps_would_cycle", _always_cycle)
        made = json.loads(await create_task(title="Cyclic", blocked_by=["x"]))
        assert made.get("error") == "blocked_by would create a dependency cycle"
