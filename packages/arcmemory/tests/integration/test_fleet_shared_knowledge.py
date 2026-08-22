"""Cross-agent acceptance tests for the shared curated-knowledge store."""

from __future__ import annotations

import pytest
from arctrust import AgentIdentity

from arcmemory.adapters import FleetSharedKnowledgeBackend, PersonalKnowledgeAdapter
from arcmemory.adapters.shared_knowledge import SharedKnowledgeAdapter


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
async def test_authorized_agent_promotes_and_peer_retrieves_from_canonical_shared_root(
    tmp_path,
) -> None:
    publisher = AgentIdentity.generate("test", "publisher")
    reader = AgentIdentity.generate("test", "reader")
    publisher_access = _Access(publisher)
    reader_access = _Access(reader)
    backend = FleetSharedKnowledgeBackend.for_arc_team(tmp_path)
    personal = PersonalKnowledgeAdapter(tmp_path / "publisher", publisher.did)
    shared = SharedKnowledgeAdapter(backend, agent_did=publisher.did, signer=publisher)

    personal_ref = await personal.save(_Draft(), publisher_access)
    source = await personal.export_for_promotion(personal_ref.identifier, publisher_access)
    shared_ref = await shared.promote(source, publisher_access)

    assert backend.root == tmp_path / "team" / "shared" / "knowledge"
    retrieved = await backend.read(shared_ref.identifier, reader_access)
    assert retrieved.content == _Draft.content
    assert (backend.root / "documents" / f"{shared_ref.identifier}.md").exists()
    assert list((backend.root / "audit").glob("*.json"))
    assert (await backend.search("release", reader_access))[0].reference == shared_ref


@pytest.mark.asyncio
async def test_unauthorized_and_no_write_down_access_fail_closed(tmp_path) -> None:
    owner = AgentIdentity.generate("test", "owner")
    intruder = AgentIdentity.generate("test", "intruder")
    backend = FleetSharedKnowledgeBackend.for_arc_team(tmp_path)
    shared = SharedKnowledgeAdapter(backend, agent_did=owner.did, signer=owner)
    owner_access = _Access(owner)
    source = type(
        "Source",
        (),
        {
            "reference": type("Reference", (), {"scope": "personal"})(),
            "digest": "sha256:4bb4ed1346878bfb3f59a0c86d7b5bd2b5b7b7dc0a21ed6b5d4fcbf6b4ec85b2",
            "content": "Release notes",
            "classification": "UNCLASSIFIED",
            "title": "Release",
            "tags": (),
            "document_type": "note",
        },
    )()
    import hashlib

    source.digest = "sha256:" + hashlib.sha256(source.content.encode()).hexdigest()
    reference = await shared.promote(source, owner_access)

    with pytest.raises(PermissionError):
        await backend.revoke(reference.identifier, _Access(intruder))
    with pytest.raises(PermissionError, match="no-write-down"):
        await shared.save(source, _Access(owner, "SECRET"))

    secret_source = type(
        "Source",
        (),
        {
            "reference": source.reference,
            "tags": source.tags,
            "document_type": source.document_type,
            "content": "Secret release procedure",
            "classification": "SECRET",
            "title": "Secret release",
        },
    )()
    secret_source.digest = "sha256:" + hashlib.sha256(secret_source.content.encode()).hexdigest()
    secret_reference = await shared.promote(secret_source, _Access(owner, "SECRET"))
    with pytest.raises(PermissionError, match="classification"):
        await backend.read(secret_reference.identifier, _Access(intruder))

    await backend.revoke(reference.identifier, owner_access)
    with pytest.raises(FileNotFoundError, match="revoked"):
        await backend.read(reference.identifier, owner_access)
