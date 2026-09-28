"""SPEC-083 COMP-023/024 — the fleet binds a shared-knowledge port on each agent.

``FleetSharedKnowledgeComposition`` start/reload binds a ``FleetSharedKnowledgePort``
(same service, the agent's access, signer and audit sink) through the agent's
public ``attach_shared_knowledge`` seam; stop withdraws it. The port acts only for
its bound caller. A refused promotion records no "allow" decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from arcmemory.adapters.personal_knowledge import PersonalKnowledgeAdapter
from arctrust import AgentIdentity
from arctrust.audit import AuditEvent

from arcteam.shared_knowledge import (
    ComposedSharedKnowledgeAgent,
    FleetSharedKnowledgeComposition,
    FleetSharedKnowledgePort,
    FleetSharedKnowledgeService,
)
from arcteam.team import Team

_VERSION = "jev-1.13.0"


@dataclass(frozen=True)
class _Access:
    caller_did: str
    clearance: str = "UNCLASSIFIED"


class _Host:
    """Records what the composition binds on a started agent."""

    def __init__(self) -> None:
        self.extensions: set[str] = set()
        self.ports: list[Any] = []

    async def attach_extension(self, extension_id: str, attachment: object) -> object:
        self.extensions.add(extension_id)  # a re-attach replaces, as on a real agent
        return None

    async def detach_extension(self, extension_id: str) -> tuple[str, ...]:
        self.extensions.discard(extension_id)
        return ()

    async def attach_shared_knowledge(self, port: Any) -> None:
        self.ports.append(port)

    async def detach_shared_knowledge(self) -> None:
        self.ports.append(None)


class _DurableSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def write_durable(self, event: AuditEvent) -> None:
        self.events.append(event)


class _Draft:
    title = "Close timing"
    content = "The month-end close runs on the third business day."
    classification = "UNCLASSIFIED"
    tags = ("insight",)
    document_type = "insight"


def _member(
    host: _Host, identity: AgentIdentity, sink: Any = None
) -> ComposedSharedKnowledgeAgent:
    return ComposedSharedKnowledgeAgent(host, object(), _Access(identity.did), identity, sink)


def _composition(tmp_path: Path, *dids: str) -> FleetSharedKnowledgeComposition:
    team = Team(id="team:t", name="t", members=list(dids), default_channel="channel://t")
    return FleetSharedKnowledgeComposition(
        team, FleetSharedKnowledgeService.for_arc_team(tmp_path)
    )


async def test_start_binds_a_port_reload_rebinds_and_stop_withdraws(tmp_path: Path) -> None:
    identity = AgentIdentity.generate("test", "a")
    host = _Host()
    member = _member(host, identity)
    composition = _composition(tmp_path, identity.did)

    await composition.start([member])
    await composition.reload(member)
    await composition.stop([member])

    first, second, withdrawn = host.ports
    assert isinstance(first, FleetSharedKnowledgePort)
    assert isinstance(second, FleetSharedKnowledgePort)
    assert second is not first
    assert withdrawn is None
    assert host.extensions == set()


async def test_non_member_gets_no_port(tmp_path: Path) -> None:
    identity = AgentIdentity.generate("test", "outsider")
    host = _Host()

    with pytest.raises(PermissionError, match="membership"):
        await _composition(tmp_path, "did:arc:test:other").start([_member(host, identity)])

    assert host.ports == []


async def _source(tmp_path: Path, identity: AgentIdentity) -> Any:
    personal = PersonalKnowledgeAdapter(tmp_path / "personal", identity.did)
    access = _Access(identity.did)
    ref = await personal.save(_Draft(), access)
    return await personal.export_for_promotion(ref.identifier, access)


async def test_bound_port_promotes_as_its_agent(tmp_path: Path) -> None:
    identity = AgentIdentity.generate("test", "a")
    sink = _DurableSink()
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    port = FleetSharedKnowledgePort(
        service, access=_Access(identity.did), signer=identity, audit_sink=sink
    )

    ref = await port.promote(
        await _source(tmp_path, identity),
        _Access(identity.did, "unclassified"),
        decision="classifier_promote",
        confidence=0.97,
        classifier_version=_VERSION,
    )

    (summary,) = await service.list_documents(_Access(identity.did))
    assert summary.reference.identifier == ref.identifier
    assert summary.owner_did == identity.did


@pytest.mark.parametrize(
    "caller",
    [_Access("did:arc:test:someone-else"), None],
    ids=["other-did", "higher-clearance"],
)
async def test_bound_port_refuses_any_other_caller(tmp_path: Path, caller: Any) -> None:
    identity = AgentIdentity.generate("test", "a")
    port = FleetSharedKnowledgePort(
        FleetSharedKnowledgeService.for_arc_team(tmp_path),
        access=_Access(identity.did),
        signer=identity,
        audit_sink=_DurableSink(),
    )
    access = caller or _Access(identity.did, "SECRET")

    with pytest.raises(PermissionError, match="bound"):
        await port.promote(
            await _source(tmp_path, identity),
            access,
            decision="classifier_promote",
            confidence=0.97,
            classifier_version=_VERSION,
        )


async def test_refused_signer_leaves_no_allow_decision(tmp_path: Path) -> None:
    """Signer does not match the caller DID: denied before the decision is recorded."""
    owner = AgentIdentity.generate("test", "owner")
    impostor = AgentIdentity.generate("test", "impostor")
    sink = _DurableSink()
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    personal = PersonalKnowledgeAdapter(tmp_path / "personal", owner.did)
    access = _Access(owner.did)
    ref = await personal.save(_Draft(), access)

    with pytest.raises(PermissionError, match="signer"):
        await service.promote(
            personal,
            ref.identifier,
            access,
            impostor,
            audit_sink=sink,
            decision="classifier_promote",
            confidence=0.97,
            classifier_version=_VERSION,
        )

    assert [e.action for e in sink.events if e.action.startswith("knowledge.promotion")] == []
    denied = [e for e in sink.events if e.outcome == "deny"]
    assert [e.action for e in denied] == ["knowledge.collection_saved"]
    assert await service.list_documents(access) == []


async def test_changed_tofu_key_leaves_no_allow_decision(tmp_path: Path) -> None:
    """A second key for an already-pinned DID is refused before any decision."""
    owner = AgentIdentity.generate("test", "owner")
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    personal = PersonalKnowledgeAdapter(tmp_path / "personal", owner.did)
    access = _Access(owner.did)
    ref = await personal.save(_Draft(), access)
    await service.promote(personal, ref.identifier, access, owner)  # pins owner's key
    sink = _DurableSink()
    trust = service.backend.root / "trusted-signers.json"
    trust.write_text(trust.read_text().replace(trust.read_text().split('"')[3], "AAAA"))

    with pytest.raises(PermissionError, match="TOFU"):
        await service.promote(
            personal,
            ref.identifier,
            access,
            owner,
            audit_sink=sink,
            decision="classifier_promote",
            confidence=0.97,
            classifier_version=_VERSION,
        )

    assert [e.action for e in sink.events if e.action.startswith("knowledge.promotion")] == []
