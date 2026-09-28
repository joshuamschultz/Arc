"""SPEC-083 — the fleet port attached after start reaches the brain's sweep.

The memory module is configured at startup with no shared port; the fleet attaches
one later. The attach hook must build a :class:`SharedKnowledgePublisher` over that
port and bind it on the live brain (never rebuild it); detach binds ``None``.

The hook only honours events the bus certifies as core-emitted: any module can
emit ``knowledge:shared_attached`` on the shared bus, and a forged emission must
neither change the held port nor reach the brain — it is ignored and audited.

The brain receives the agent's durable audit sink so its promotion egress record
can be written durably.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from arctrust import AgentIdentity

from arcagent.knowledge import SHARED_KNOWLEDGE_ATTACHED, SHARED_KNOWLEDGE_DETACHED
from arcagent.modules.memory import _runtime
from arcagent.modules.memory.capabilities import (
    on_shared_knowledge_attached,
    on_shared_knowledge_detached,
)

_FAKE_BACKEND = "spec083_late_bind_backend"


@pytest.fixture(autouse=True)
def _reset() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


class _BindableBrain:
    def __init__(self) -> None:
        self.bound: list[object | None] = []

    def bind_promotion_publisher(self, publisher: object | None) -> None:
        self.bound.append(publisher)

    async def capture(self, text: str, **_: Any) -> None:  # pragma: no cover
        return None

    async def retrieve(self, query: str, **_: Any) -> str:  # pragma: no cover
        return ""

    async def consolidate(self, **_: Any) -> dict[str, object]:  # pragma: no cover
        return {}

    async def refresh_index(self, **_: Any) -> None:  # pragma: no cover
        return None

    async def rebuild_index(self, **_: Any) -> None:  # pragma: no cover
        return None


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> tuple[list[dict[str, Any]], _BindableBrain]:
    contexts: list[dict[str, Any]] = []
    brain = _BindableBrain()
    module = types.ModuleType(_FAKE_BACKEND)

    def build_brain(context: dict[str, Any]) -> _BindableBrain:
        contexts.append(context)
        return brain

    module.build_brain = build_brain  # type: ignore[attr-defined]  # reason: synthetic module
    monkeypatch.setitem(sys.modules, _FAKE_BACKEND, module)
    return contexts, brain


class _Port:
    async def save(self, draft: Any, access: Any) -> Any:  # pragma: no cover
        raise PermissionError

    async def read(self, reference: str, access: Any) -> Any:  # pragma: no cover
        raise LookupError

    async def search(self, query: str, access: Any) -> list[Any]:  # pragma: no cover
        return []

    async def promote(self, source: Any, access: Any, **_: Any) -> Any:  # pragma: no cover
        raise PermissionError

    async def revoke(self, reference: str, access: Any) -> None:  # pragma: no cover
        return None


class _Telemetry:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def audit_event(self, event_type: str, details: dict[str, Any]) -> None:
        self.events.append((event_type, details))


class _Ctx:
    """The hook's view of one bus event."""

    def __init__(
        self, event: str, agent_did: str, data: Mapping[str, Any], *, core_certified: bool
    ) -> None:
        self.event = event
        self.agent_did = agent_did
        self.data = dict(data)
        self.core_certified = core_certified


def _configure(
    workspace: Path,
    identity: AgentIdentity,
    *,
    promotion: bool = True,
    telemetry: Any = None,
    audit_sink: Any = None,
) -> None:
    _runtime.configure(
        config={"brain": _FAKE_BACKEND, "promotion": {"enabled": promotion}},
        workspace=workspace,
        agent_did=identity.did,
        identity=identity,
        telemetry=telemetry,
        audit_sink=audit_sink,
    )


def _identity() -> AgentIdentity:
    return AgentIdentity.generate("test", "late")


async def test_attach_after_start_binds_a_publisher_on_the_live_brain(
    tmp_path: Path, backend: tuple[list[dict[str, Any]], _BindableBrain]
) -> None:
    from arcagent.modules.memory.promotion import SharedKnowledgePublisher

    contexts, brain = backend
    identity = _identity()
    _configure(tmp_path, identity)

    await on_shared_knowledge_attached(
        _Ctx(SHARED_KNOWLEDGE_ATTACHED, identity.did, {"port": _Port()}, core_certified=True)
    )

    assert len(contexts) == 1, "the brain must not be rebuilt"
    (publisher,) = brain.bound
    assert isinstance(publisher, SharedKnowledgePublisher)


async def test_detach_binds_no_publisher(
    tmp_path: Path, backend: tuple[list[dict[str, Any]], _BindableBrain]
) -> None:
    _, brain = backend
    identity = _identity()
    _configure(tmp_path, identity)
    await on_shared_knowledge_attached(
        _Ctx(SHARED_KNOWLEDGE_ATTACHED, identity.did, {"port": _Port()}, core_certified=True)
    )

    await on_shared_knowledge_detached(
        _Ctx(SHARED_KNOWLEDGE_DETACHED, identity.did, {}, core_certified=True)
    )

    assert brain.bound[-1] is None
    assert _runtime.state_for(identity.did).shared_knowledge is None


async def test_attach_with_promotion_disabled_holds_the_port_but_binds_nothing(
    tmp_path: Path, backend: tuple[list[dict[str, Any]], _BindableBrain]
) -> None:
    _, brain = backend
    identity = _identity()
    port = _Port()
    _configure(tmp_path, identity, promotion=False)

    await on_shared_knowledge_attached(
        _Ctx(SHARED_KNOWLEDGE_ATTACHED, identity.did, {"port": port}, core_certified=True)
    )

    assert _runtime.state_for(identity.did).shared_knowledge is port
    assert brain.bound == []


@pytest.mark.parametrize("event", [SHARED_KNOWLEDGE_ATTACHED, SHARED_KNOWLEDGE_DETACHED])
async def test_uncertified_event_is_ignored_and_audited(
    tmp_path: Path, backend: tuple[list[dict[str, Any]], _BindableBrain], event: str
) -> None:
    _, brain = backend
    identity = _identity()
    telemetry = _Telemetry()
    genuine = _Port()
    _configure(tmp_path, identity, telemetry=telemetry)
    await on_shared_knowledge_attached(
        _Ctx(SHARED_KNOWLEDGE_ATTACHED, identity.did, {"port": genuine}, core_certified=True)
    )
    bound_before = list(brain.bound)

    handler = (
        on_shared_knowledge_attached
        if event == SHARED_KNOWLEDGE_ATTACHED
        else on_shared_knowledge_detached
    )
    await handler(_Ctx(event, identity.did, {"port": _Port()}, core_certified=False))

    assert _runtime.state_for(identity.did).shared_knowledge is genuine
    assert brain.bound == bound_before
    forged = [d for name, d in telemetry.events if name == "memory.shared_knowledge_forged"]
    assert forged == [{"agent_did": identity.did, "event": event, "outcome": "deny"}]


def test_brain_receives_the_agents_durable_audit_sink(
    tmp_path: Path, backend: tuple[list[dict[str, Any]], _BindableBrain]
) -> None:
    contexts, _ = backend
    sink = object()

    _configure(tmp_path, _identity(), audit_sink=sink)

    assert contexts[0]["audit_sink"] is sink


def test_reconfigure_keeps_the_attached_port(
    tmp_path: Path, backend: tuple[list[dict[str, Any]], _BindableBrain]
) -> None:
    """A module reload rebuilds the state; the fleet's port must survive it."""
    from arcagent.modules.memory.promotion import SharedKnowledgePublisher

    contexts, _ = backend
    identity = _identity()
    port = _Port()
    _configure(tmp_path, identity)
    _runtime.attach_shared_knowledge(identity.did, port)

    _configure(tmp_path, identity)

    assert _runtime.state_for(identity.did).shared_knowledge is port
    assert isinstance(contexts[-1]["promotion_publisher"], SharedKnowledgePublisher)
