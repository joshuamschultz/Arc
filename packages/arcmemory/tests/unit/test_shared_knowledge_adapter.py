"""Tests for team-agnostic signed knowledge collection mechanics."""

from __future__ import annotations

import pytest
from arctrust import AgentIdentity, AuditEvent

from arcmemory.adapters.shared_knowledge import SharedKnowledgeAdapter


class _Backend:
    def __init__(self) -> None:
        self.saved: list[object] = []

    async def save(self, draft: object, access: object) -> object:
        del access
        self.saved.append(draft)
        return type(
            "Ref", (), {"scope": "shared", "identifier": "collection-1", "digest": draft.digest}
        )()

    async def read(self, reference: str, access: object) -> object:
        return {"reference": reference, "access": access}

    async def search(self, query: str, access: object) -> list[object]:
        return [{"query": query, "access": access}]

    async def revoke(self, reference: str, access: object) -> None:
        del reference, access


class _Access:
    caller_did = "did:arc:test:caller"
    clearance = "UNCLASSIFIED"


class _Draft:
    title = "Shared fact"
    content = "shared fact"
    classification = "UNCLASSIFIED"
    tags = ("alpha",)
    document_type = "note"


class _Sink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_collection_signs_and_delegates_to_the_injected_backend() -> None:
    backend = _Backend()
    identity = AgentIdentity.generate("test", "one")
    adapter = SharedKnowledgeAdapter(backend, owner_did=identity.did, signer=identity)

    result = await adapter.save(_Draft(), _Access())

    assert result.scope == "shared"
    saved = backend.saved[0]
    assert saved.title == "Shared fact"
    assert saved.owner_did == identity.did
    assert saved.signature


@pytest.mark.asyncio
async def test_collection_rejects_invalid_documents_before_backend_call() -> None:
    backend = _Backend()
    identity = AgentIdentity.generate("test", "one")
    adapter = SharedKnowledgeAdapter(backend, owner_did=identity.did, signer=identity)
    draft = _Draft()
    draft.content = ""

    with pytest.raises(ValueError, match="content"):
        await adapter.save(draft, _Access())
    assert backend.saved == []


@pytest.mark.asyncio
async def test_collection_does_not_embed_fleet_authorization_policy() -> None:
    backend = _Backend()
    identity = AgentIdentity.generate("test", "one")
    adapter = SharedKnowledgeAdapter(backend, owner_did=identity.did, signer=identity)

    await adapter.save(_Draft(), _Access())

    assert len(backend.saved) == 1


@pytest.mark.asyncio
async def test_collection_emits_metadata_only_audit_events() -> None:
    backend = _Backend()
    sink = _Sink()
    identity = AgentIdentity.generate("test", "audit")
    adapter = SharedKnowledgeAdapter(
        backend, owner_did=identity.did, signer=identity, audit_sink=sink
    )

    await adapter.save(_Draft(), _Access())
    await adapter.read("shared-1", _Access())
    await adapter.search("private query", _Access())
    await adapter.revoke("shared-1", _Access())

    assert [event.action for event in sink.events] == [
        "knowledge.collection_saved",
        "knowledge.collection_retrieved",
        "knowledge.collection_searched",
        "knowledge.collection_revoked",
    ]
    assert sink.events[2].payload_hash
    assert "private query" not in str(sink.events[2].model_dump())
