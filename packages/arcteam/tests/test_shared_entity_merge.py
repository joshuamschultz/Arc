"""Entity promotion fails closed until signed contributor merge is available."""

from __future__ import annotations

from pathlib import Path

import pytest
from arcmemory.adapters.personal_knowledge import PersonalKnowledgeAdapter
from arctrust import AgentIdentity

from arcteam.shared_knowledge import FleetSharedKnowledgeService, SharedKnowledgeUnavailableError


class _Access:
    def __init__(self, identity: AgentIdentity, clearance: str = "UNCLASSIFIED") -> None:
        self.caller_did = identity.did
        self.clearance = clearance


class _EntityDraft:
    """A promotable ENTITY document — same real-world entity, differing fact."""

    def __init__(self, content: str) -> None:
        self.title = "Acme"
        self.content = content
        self.classification = "UNCLASSIFIED"
        self.tags = ("entity", "vendor")
        self.document_type = "entity"


def _entity_docs(service: FleetSharedKnowledgeService) -> list[Path]:
    return sorted((service.backend.root / "documents").glob("*.md"))


async def _promote_entity(
    service: FleetSharedKnowledgeService,
    tmp_path: Path,
    identity: AgentIdentity,
    content: str,
) -> object:
    access = _Access(identity)
    personal = PersonalKnowledgeAdapter(tmp_path / identity.did.replace("/", "_"), identity.did)
    ref = await personal.save(_EntityDraft(content), access)
    return await service.promote(personal, ref.identifier, access, identity)


async def test_two_agents_cannot_create_duplicate_entity_documents(
    tmp_path: Path,
) -> None:
    """Both contributors receive typed unavailable and no shared entity appears."""
    agent_a = AgentIdentity.generate("test", "agent-a")
    agent_b = AgentIdentity.generate("test", "agent-b")
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)

    for identity, content in (
        (agent_a, "Acme\nrole: vendor\nlead_time: 2 weeks"),
        (agent_b, "Acme\nrole: partner\nlead_time: 2 weeks"),
    ):
        with pytest.raises(SharedKnowledgeUnavailableError):
            await _promote_entity(service, tmp_path, identity, content)
    assert _entity_docs(service) == []


async def test_conflicting_entity_facts_are_not_exposed_without_provenance(
    tmp_path: Path,
) -> None:
    """Conflicting values cannot leak as unprovenanced shared documents."""
    agent_a = AgentIdentity.generate("test", "agent-a")
    agent_b = AgentIdentity.generate("test", "agent-b")
    reader = AgentIdentity.generate("test", "reader")
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)

    for identity, content in ((agent_a, "Acme\nrole: vendor"), (agent_b, "Acme\nrole: partner")):
        with pytest.raises(SharedKnowledgeUnavailableError):
            await _promote_entity(service, tmp_path, identity, content)
    assert await service.list_documents(_Access(reader)) == []


async def test_repromoting_entity_stays_unavailable(tmp_path: Path) -> None:
    """Repeated requests cannot bypass the unavailable entity boundary."""
    agent_a = AgentIdentity.generate("test", "agent-a")
    agent_b = AgentIdentity.generate("test", "agent-b")
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)

    for identity, content in (
        (agent_a, "Acme\nrole: vendor"),
        (agent_b, "Acme\nrole: partner"),
        (agent_a, "Acme\nrole: vendor"),
    ):
        with pytest.raises(SharedKnowledgeUnavailableError):
            await _promote_entity(service, tmp_path, identity, content)
    assert _entity_docs(service) == []
