from __future__ import annotations

import hashlib

import pytest
from arctrust import AuditEvent

from arcmemory.adapters.shared_knowledge import SharedKnowledgeAdapter


class _Backend:
    def __init__(self) -> None:
        self.saved = []

    async def save(self, draft, access):
        self.saved.append((draft, access))
        return type(
            "Ref", (), {"scope": "shared", "identifier": "team-1", "digest": draft.digest}
        )()


class _Access:
    caller_did = "did:arc:one"
    clearance = "UNCLASSIFIED"


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
    adapter = SharedKnowledgeAdapter(backend, agent_did="did:arc:one", audit_sink=sink)

    result = await adapter.promote(_source(), _Access())

    assert result.scope == "shared"
    assert backend.saved[0][0].title == "Shared fact"
    assert backend.saved[0][0].content == "shared fact"
    assert sink.events[-1].action == "knowledge.promoted"


@pytest.mark.asyncio
async def test_promotion_rejects_tampered_source_before_backend_call() -> None:
    backend = _Backend()
    source = _source()
    source.content = "tampered"
    adapter = SharedKnowledgeAdapter(backend, agent_did="did:arc:one")

    with pytest.raises(ValueError, match="digest"):
        await adapter.promote(source, _Access())
    assert backend.saved == []


@pytest.mark.asyncio
async def test_promotion_requires_agent_clearance_and_personal_source() -> None:
    backend = _Backend()
    adapter = SharedKnowledgeAdapter(backend, agent_did="did:arc:one")
    access = type("Other", (), {"caller_did": "did:arc:two", "clearance": "UNCLASSIFIED"})()

    with pytest.raises(PermissionError):
        await adapter.promote(_source(), access)
    source = _source()
    source.reference.scope = "shared"
    with pytest.raises(ValueError, match="personal"):
        await adapter.promote(source, _Access())
    assert backend.saved == []
