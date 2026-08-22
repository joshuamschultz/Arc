from __future__ import annotations

import hashlib

import pytest
from arctrust import AgentIdentity, AuditEvent

from arcmemory.adapters.shared_knowledge import SharedKnowledgeAdapter


class _Backend:
    def __init__(self) -> None:
        self.saved = []

    async def save(self, draft, access):
        self.saved.append((draft, access))
        return type(
            "Ref", (), {"scope": "shared", "identifier": "team-1", "digest": draft.digest}
        )()

    async def read(self, reference, access):
        return {"reference": reference}

    async def search(self, query, access):
        return [{"query": query}]

    async def revoke(self, reference, access):
        return None


class _Access:
    def __init__(self, caller_did: str, clearance: str = "UNCLASSIFIED") -> None:
        self.caller_did = caller_did
        self.clearance = clearance


class _Sink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _source(content: str = "shared fact"):
    digest = "sha256:" + hashlib.sha256(content.encode()).hexdigest()
    reference = type("Ref", (), {"scope": "personal", "identifier": "p-1", "digest": digest})()
    return type(
        "Source",
        (),
        {
            "reference": reference,
            "digest": digest,
            "content": content,
            "classification": "UNCLASSIFIED",
            "title": "Shared fact",
            "tags": ("alpha",),
            "document_type": "note",
        },
    )()


@pytest.mark.asyncio
async def test_promotion_verifies_digest_and_delegates_to_injected_shared_backend() -> None:
    backend = _Backend()
    sink = _Sink()
    identity = AgentIdentity.generate("test", "one")
    adapter = SharedKnowledgeAdapter(
        backend, agent_did=identity.did, signer=identity, audit_sink=sink
    )

    result = await adapter.promote(_source(), _Access(identity.did))

    assert result.scope == "shared"
    assert backend.saved[0][0].title == "Shared fact"
    assert backend.saved[0][0].content == "shared fact"
    assert sink.events[-1].action == "knowledge.promoted"


@pytest.mark.asyncio
async def test_promotion_rejects_tampered_source_before_backend_call() -> None:
    backend = _Backend()
    identity = AgentIdentity.generate("test", "one")
    source = _source()
    source.content = "tampered"
    adapter = SharedKnowledgeAdapter(backend, agent_did=identity.did, signer=identity)

    with pytest.raises(ValueError, match="digest"):
        await adapter.promote(source, _Access(identity.did))
    assert backend.saved == []


@pytest.mark.asyncio
async def test_promotion_requires_agent_clearance_and_personal_source() -> None:
    backend = _Backend()
    identity = AgentIdentity.generate("test", "one")
    adapter = SharedKnowledgeAdapter(backend, agent_did=identity.did, signer=identity)
    access = _Access(AgentIdentity.generate("test", "two").did)

    with pytest.raises(PermissionError):
        await adapter.promote(_source(), access)
    source = _source()
    source.reference.scope = "shared"
    with pytest.raises(ValueError, match="personal"):
        await adapter.promote(source, _Access(identity.did))
    assert backend.saved == []


@pytest.mark.asyncio
async def test_shared_operations_emit_metadata_only_audit_events() -> None:
    backend = _Backend()
    sink = _Sink()
    identity = AgentIdentity.generate("test", "audit")
    adapter = SharedKnowledgeAdapter(
        backend, agent_did=identity.did, signer=identity, audit_sink=sink
    )
    access = _Access(identity.did)

    await adapter.read("shared-1", access)
    await adapter.search("private query", access)
    await adapter.revoke("shared-1", access)

    assert [event.action for event in sink.events] == [
        "knowledge.retrieved",
        "knowledge.searched",
        "knowledge.revoked",
    ]
    assert sink.events[1].payload_hash
    assert "private query" not in str(sink.events[1].model_dump())
