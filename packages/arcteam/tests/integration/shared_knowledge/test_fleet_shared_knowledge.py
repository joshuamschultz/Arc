"""Cross-agent acceptance tests for fleet-owned curated shared knowledge."""

from __future__ import annotations

import pytest
from arcmemory.adapters.personal_knowledge import PersonalKnowledgeAdapter
from arctrust import AgentIdentity

from arcteam.shared_knowledge import FleetSharedKnowledgeService


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
