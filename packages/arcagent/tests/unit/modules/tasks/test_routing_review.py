"""SPEC-056 Phase 3/4 — auto-routing, review gate, operator notifications (TDD).

Essential coverage: unassigned tasks route to the least-loaded / capability-
matched agent; a review-gated task completes into ``review`` (not ``done``);
and operator notifications fire best-effort on key transitions. Uses a real
``TaskStore`` with a fake registry (roster) and a fake messenger (inbox).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from arcteam.types import Entity, EntityStatus, EntityType
from arctrust import AgentIdentity

_A = "did:arc:test:agent/aaaaaaaa"
_B = "did:arc:test:agent/bbbbbbbb"


def _agent(name: str, did: str, caps: tuple[str, ...] = ()) -> Entity:
    return Entity(
        did=did, handle=name, id=did, name=name,
        type=EntityType.AGENT, capabilities=list(caps), status=EntityStatus.active,
    )


class _FakeRegistry:
    def __init__(self, entities: list[Entity]) -> None:
        self._entities = entities

    async def list_entities(self, role: str | None = None) -> list[Entity]:
        return self._entities


class _FakeMessenger:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, message: Any) -> Any:
        self.sent.append(message)
        return message


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
    yield st, identity
    _runtime.reset()


async def _seed(st: Any, **fields: Any) -> Any:
    from arcstore.tasks import Task

    from arcagent.modules.tasks import _runtime

    await _runtime.ensure_store()
    return await st.store.create(Task(**fields))


@pytest.mark.asyncio
class TestAutoRouting:
    async def test_routes_to_least_loaded_agent(self, state: Any) -> None:
        from arcagent.modules.tasks.capabilities import _route_unassigned

        st, identity = state
        st.registry = _FakeRegistry([_agent("alice", _A), _agent("bob", _B)])
        # alice already has one in-flight task -> bob is least loaded.
        await _seed(st, id="busy", title="Busy", creator_did=_A, owner_did=_A, status="in_progress")
        await _seed(st, id="free", title="Route me", creator_did=identity.did, status="backlog")

        await _route_unassigned(st, identity.did)

        routed = await st.store.get("free")
        assert routed is not None and routed.owner_did == _B and routed.status == "todo"

    async def test_capability_match_beats_load(self, state: Any) -> None:
        from arcagent.modules.tasks.capabilities import _route_unassigned

        st, identity = state
        # bob is busier but matches the task's capability tag; alice is idle.
        st.registry = _FakeRegistry([_agent("alice", _A), _agent("bob", _B, caps=("ml",))])
        await _seed(st, id="busy", title="Busy", creator_did=_B, owner_did=_B, status="in_progress")
        await _seed(st, id="t", title="ML task", creator_did=identity.did, status="backlog", tags=["ml"])

        await _route_unassigned(st, identity.did)

        routed = await st.store.get("t")
        assert routed is not None and routed.owner_did == _B  # capability match wins

    async def test_no_registry_is_noop(self, state: Any) -> None:
        from arcagent.modules.tasks.capabilities import _route_unassigned

        st, identity = state
        st.registry = None
        await _seed(st, id="t", title="Orphan", creator_did=identity.did, status="backlog")

        await _route_unassigned(st, identity.did)

        task = await st.store.get("t")
        assert task is not None and task.owner_did is None  # unrouted


@pytest.mark.asyncio
class TestReviewGate:
    async def test_review_required_completes_into_review(self, state: Any) -> None:
        from arcagent.modules.tasks.capabilities import complete_task

        st, identity = state
        st.messenger = _FakeMessenger()
        await _seed(
            st, id="r", title="Gated", creator_did=identity.did, owner_did=identity.did,
            status="in_progress", requires_review=True,
        )
        result = json.loads(await complete_task(id="r", resolution="did the thing"))
        assert result["status"] == "review"  # NOT done

        task = await st.store.get("r")
        assert task is not None and task.status == "review"
        assert any("needs review" in str(m.body) for m in st.messenger.sent)

    async def test_no_review_completes_to_done(self, state: Any) -> None:
        from arcagent.modules.tasks.capabilities import complete_task

        st, identity = state
        st.messenger = _FakeMessenger()
        await _seed(
            st, id="d", title="Plain", creator_did=identity.did, owner_did=identity.did,
            status="in_progress",
        )
        result = json.loads(await complete_task(id="d", resolution="done"))
        assert result["status"] == "done"
        assert any("done" in str(m.body) for m in st.messenger.sent)


@pytest.mark.asyncio
class TestOperatorNotify:
    async def test_fail_notifies_operator_as_alert(self, state: Any) -> None:
        from arcagent.modules.tasks.capabilities import fail_task

        st, identity = state
        st.messenger = _FakeMessenger()
        await _seed(
            st, id="f", title="Doomed", creator_did=identity.did, owner_did=identity.did,
            status="in_progress",
        )
        await fail_task(id="f", resolution="nope")
        assert st.messenger.sent
        msg = st.messenger.sent[-1]
        assert "failed" in str(msg.body)
        assert str(msg.to[0]) == "user://operator"

    async def test_notify_disabled_sends_nothing(self, state: Any) -> None:
        from arcagent.modules.tasks.capabilities import fail_task

        st, identity = state
        st.config = st.config.model_copy(update={"notify": False})
        st.messenger = _FakeMessenger()
        await _seed(
            st, id="f", title="Quiet", creator_did=identity.did, owner_did=identity.did,
            status="in_progress",
        )
        await fail_task(id="f", resolution="nope")
        assert st.messenger.sent == []
