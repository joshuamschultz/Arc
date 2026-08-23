"""Cross-agent acceptance tests for fleet-owned curated shared knowledge."""

from __future__ import annotations

import arcagent
import pytest
from arcmemory.adapters.personal_knowledge import PersonalKnowledgeAdapter
from arctrust import AgentIdentity

from arcteam.shared_knowledge import (
    ComposedSharedKnowledgeAgent,
    FleetSharedKnowledgeComposition,
    FleetSharedKnowledgeService,
)
from arcteam.team import Team


class _Access:
    def __init__(self, identity: AgentIdentity, clearance: str = "UNCLASSIFIED") -> None:
        self.caller_did = identity.did
        self.clearance = clearance


class _Draft:
    title = "Release procedure"
    content = "Run the verified release checklist."
    classification = "UNCLASSIFIED"
    tags = ("release",)
    document_type = "procedure"


@pytest.mark.asyncio
async def test_signed_promotion_allows_an_authorized_peer_to_retrieve(tmp_path) -> None:
    publisher = AgentIdentity.generate("test", "publisher")
    reader = AgentIdentity.generate("test", "reader")
    publisher_access = _Access(publisher)
    reader_access = _Access(reader)
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    personal = PersonalKnowledgeAdapter(tmp_path / "publisher", publisher.did)

    personal_ref = await personal.save(_Draft(), publisher_access)
    shared_ref = await service.promote(
        personal, personal_ref.identifier, publisher_access, publisher
    )

    assert service.backend.root == tmp_path / "team" / "shared" / "knowledge"
    retrieved = await service.read(shared_ref.identifier, reader_access)
    assert retrieved.content == _Draft.content
    assert (service.backend.root / "documents" / f"{shared_ref.identifier}.md").exists()
    assert list((service.backend.root / "audit").glob("*.json"))
    assert (await service.search("release", reader_access))[0].reference == shared_ref


@pytest.mark.asyncio
async def test_no_read_up_and_revocation_fail_closed(tmp_path) -> None:
    owner = AgentIdentity.generate("test", "owner")
    reader = AgentIdentity.generate("test", "reader")
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    owner_access = _Access(owner, "SECRET")
    reader_access = _Access(reader, "UNCLASSIFIED")
    personal = PersonalKnowledgeAdapter(tmp_path / "owner", owner.did)
    draft = _Draft()
    draft.classification = "SECRET"

    personal_ref = await personal.save(draft, owner_access)
    shared_ref = await service.promote(personal, personal_ref.identifier, owner_access, owner)

    with pytest.raises(PermissionError, match="classification"):
        await service.read(shared_ref.identifier, reader_access)
    await service.revoke(shared_ref.identifier, owner_access)
    with pytest.raises(FileNotFoundError, match="revoked"):
        await service.read(shared_ref.identifier, owner_access)


def _agent_config(tmp_path, name: str) -> arcagent.ArcAgentConfig:
    return arcagent.ArcAgentConfig.model_validate(
        {
            "agent": {
                "name": name,
                "org": "test",
                "type": "executor",
                "workspace": str(tmp_path / name / "workspace"),
            },
            "llm": {"model": "test/model"},
            "identity": {"key_dir": str(tmp_path / name / "keys")},
            "telemetry": {"enabled": False},
        }
    )


@pytest.mark.asyncio
async def test_composition_installs_reloads_and_removes_tools_on_two_started_agents(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    publisher = arcagent.ArcAgent(_agent_config(tmp_path, "publisher"))
    reader = arcagent.ArcAgent(_agent_config(tmp_path, "reader"))
    await publisher.startup()
    await reader.startup()
    try:
        shared_names = {
            "shared_knowledge_promote",
            "shared_knowledge_retrieve",
            "shared_knowledge_search",
            "shared_knowledge_revoke",
        }
        assert not shared_names & {tool.name for tool in publisher.registered_tools}
        assert not shared_names & {tool.name for tool in reader.registered_tools}

        publisher_access = arcagent.KnowledgeAccess(publisher.did, "UNCLASSIFIED")
        reader_access = arcagent.KnowledgeAccess(reader.did, "UNCLASSIFIED")
        publisher_personal = PersonalKnowledgeAdapter(
            tmp_path / "publisher-personal", publisher.did
        )
        reader_personal = PersonalKnowledgeAdapter(tmp_path / "reader-personal", reader.did)
        team = Team(
            id="team:test",
            name="test",
            members=[publisher.did, reader.did],
            default_channel="channel://test",
        )
        composition = FleetSharedKnowledgeComposition(
            team, FleetSharedKnowledgeService.for_arc_team(tmp_path)
        )
        members = [
            ComposedSharedKnowledgeAgent(
                publisher, publisher_personal, publisher_access, publisher.extension_signer
            ),
            ComposedSharedKnowledgeAgent(
                reader, reader_personal, reader_access, reader.extension_signer
            ),
        ]

        await composition.start(members)
        assert shared_names <= {tool.name for tool in publisher.registered_tools}
        assert shared_names <= {tool.name for tool in reader.registered_tools}

        await composition.reload(members[0])
        assert shared_names <= {tool.name for tool in publisher.registered_tools}

        await composition.stop(members)
        assert not shared_names & {tool.name for tool in publisher.registered_tools}
        assert not shared_names & {tool.name for tool in reader.registered_tools}
    finally:
        await publisher.shutdown()
        await reader.shutdown()
