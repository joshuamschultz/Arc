from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from arcagent.knowledge import KnowledgeAccess, KnowledgeDocument, KnowledgeDraft, KnowledgeHit, KnowledgeRef
from arcagent.modules.knowledge import _runtime
from arcagent.modules.knowledge.capabilities import knowledge_read, knowledge_save, knowledge_search


@dataclass
class FakePort:
    calls: list[tuple[str, object]] = field(default_factory=list)

    async def save(self, draft: KnowledgeDraft, access: KnowledgeAccess) -> KnowledgeRef:
        self.calls.append(("save", access))
        return KnowledgeRef(scope="personal", identifier="one", digest="sha256:one")

    async def read(self, reference: str, access: KnowledgeAccess) -> KnowledgeDocument:
        self.calls.append(("read", access))
        return KnowledgeDocument(reference=KnowledgeRef("personal", reference, "sha256:one"), title="T", content="body", classification="UNCLASSIFIED", tags=())

    async def search(self, query: str, access: KnowledgeAccess) -> list[KnowledgeHit]:
        self.calls.append(("search", access))
        return []


@pytest.mark.asyncio
async def test_scope_routes_to_the_selected_port_without_fallback() -> None:
    personal, shared = FakePort(), FakePort()
    _runtime.configure(personal_knowledge_port=personal, shared_knowledge_port=shared, agent_did="did:arc:agent", clearance="SECRET")
    assert '"identifier": "one"' in await knowledge_save("personal", "Title", "body")
    assert len(personal.calls) == 1
    assert not shared.calls


@pytest.mark.asyncio
async def test_unavailable_shared_scope_does_not_fall_back_to_personal() -> None:
    personal = FakePort()
    _runtime.configure(personal_knowledge_port=personal, shared_knowledge_port=None, agent_did="did:arc:agent", clearance="SECRET")
    with pytest.raises(RuntimeError, match="shared knowledge is unavailable"):
        await knowledge_search("shared", "needle")
    assert not personal.calls


@pytest.mark.asyncio
async def test_tools_never_accept_identity_or_clearance_from_llm_arguments() -> None:
    _runtime.configure(personal_knowledge_port=FakePort(), shared_knowledge_port=None, agent_did="did:arc:authoritative", clearance="TOP_SECRET")
    await knowledge_read("personal", "one")
    assert _runtime.state().access == KnowledgeAccess(caller_did="did:arc:authoritative", clearance="TOP_SECRET")


@pytest.mark.asyncio
async def test_two_agents_concurrently_keep_ports_and_authority_isolated() -> None:
    first, second = FakePort(), FakePort()
    _runtime.configure(personal_knowledge_port=first, agent_did="did:arc:first")
    first_state = _runtime.state()
    _runtime.configure(personal_knowledge_port=second, agent_did="did:arc:second")
    second_state = _runtime.state()

    async def save(state: _runtime._State) -> str:
        _runtime.bind(state)
        return await knowledge_save("personal", "Title", "body")

    await asyncio.gather(save(first_state), save(second_state))
    assert first.calls[0][1] == KnowledgeAccess("did:arc:first", "UNCLASSIFIED")
    assert second.calls[0][1] == KnowledgeAccess("did:arc:second", "UNCLASSIFIED")
