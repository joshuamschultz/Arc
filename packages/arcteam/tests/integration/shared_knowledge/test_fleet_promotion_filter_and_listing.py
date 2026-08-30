"""Operator filter, audit emission, and the fleet-scoped read surface (H-027)."""

from __future__ import annotations

import pytest
from arcmemory.adapters.personal_knowledge import PersonalKnowledgeAdapter
from arctrust import AgentIdentity, AuditEvent

from arcteam.shared_knowledge import FleetSharedKnowledgeService


class _Access:
    def __init__(self, identity: AgentIdentity, clearance: str = "UNCLASSIFIED") -> None:
        self.caller_did = identity.did
        self.clearance = clearance


class _Draft:
    def __init__(
        self,
        *,
        title: str = "Release procedure",
        content: str = "Run the verified release checklist.",
        classification: str = "UNCLASSIFIED",
        tags: tuple[str, ...] = ("release",),
        document_type: str = "procedure",
    ) -> None:
        self.title = title
        self.content = content
        self.classification = classification
        self.tags = tags
        self.document_type = document_type


class _CapturingSink:
    """A real ``arctrust`` audit sink that records every event written to it."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


async def _promote(service: FleetSharedKnowledgeService, tmp_path, draft, *, audit_sink=None):
    identity = AgentIdentity.generate("test", "publisher")
    access = _Access(identity)
    personal = PersonalKnowledgeAdapter(tmp_path / "publisher", identity.did)
    personal_ref = await personal.save(draft, access)
    return await service.promote(
        personal, personal_ref.identifier, access, identity, audit_sink=audit_sink
    )


@pytest.mark.asyncio
async def test_for_team_root_resolves_shared_knowledge_root(tmp_path) -> None:
    service = FleetSharedKnowledgeService.for_team_root(tmp_path / "team")
    assert service.backend.root == (tmp_path / "team" / "shared" / "knowledge").resolve()


@pytest.mark.asyncio
async def test_operator_filter_refuses_a_document_type_not_allowed(tmp_path) -> None:
    service = FleetSharedKnowledgeService.for_arc_team(
        tmp_path, promotable_document_types={"procedure"}
    )
    with pytest.raises(PermissionError, match="not promotable"):
        await _promote(service, tmp_path, _Draft(document_type="note"))


@pytest.mark.asyncio
async def test_operator_filter_allows_a_permitted_document_type(tmp_path) -> None:
    service = FleetSharedKnowledgeService.for_arc_team(
        tmp_path, promotable_document_types={"procedure"}
    )
    reference = await _promote(service, tmp_path, _Draft(document_type="procedure"))
    assert reference.scope == "shared"


@pytest.mark.asyncio
async def test_no_filter_shares_any_type(tmp_path) -> None:
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    reference = await _promote(service, tmp_path, _Draft(document_type="note"))
    assert reference.scope == "shared"


@pytest.mark.asyncio
async def test_promote_emits_a_collection_saved_audit_event(tmp_path) -> None:
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    sink = _CapturingSink()
    await _promote(service, tmp_path, _Draft(), audit_sink=sink)
    actions = {event.action: event for event in sink.events}
    assert "knowledge.collection_saved" in actions
    saved = actions["knowledge.collection_saved"]
    assert saved.outcome == "allow"
    assert saved.actor_did.startswith("did:arc:")


@pytest.mark.asyncio
async def test_list_documents_is_owner_scoped_and_classification_filtered(tmp_path) -> None:
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)

    unclassified_owner = AgentIdentity.generate("test", "alpha")
    secret_owner = AgentIdentity.generate("test", "bravo")
    reader = _Access(AgentIdentity.generate("test", "reader"), "UNCLASSIFIED")

    alpha_access = _Access(unclassified_owner)
    alpha_personal = PersonalKnowledgeAdapter(tmp_path / "alpha", unclassified_owner.did)
    alpha_ref = await alpha_personal.save(_Draft(title="Alpha note"), alpha_access)
    await service.promote(alpha_personal, alpha_ref.identifier, alpha_access, unclassified_owner)

    secret_access = _Access(secret_owner, "SECRET")
    secret_personal = PersonalKnowledgeAdapter(tmp_path / "bravo", secret_owner.did)
    secret_ref = await secret_personal.save(
        _Draft(title="Bravo secret", classification="SECRET"), secret_access
    )
    await service.promote(secret_personal, secret_ref.identifier, secret_access, secret_owner)

    summaries = await service.list_documents(reader)
    owners = {summary.owner_did for summary in summaries}
    titles = {summary.title for summary in summaries}
    assert unclassified_owner.did in owners
    assert secret_owner.did not in owners  # no-read-up hides the SECRET doc
    assert "Alpha note" in titles
    assert "Bravo secret" not in titles


@pytest.mark.asyncio
async def test_list_documents_excludes_a_revoked_document(tmp_path) -> None:
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    owner = AgentIdentity.generate("test", "owner")
    owner_access = _Access(owner)
    personal = PersonalKnowledgeAdapter(tmp_path / "owner", owner.did)
    ref = await personal.save(_Draft(title="Doomed"), owner_access)
    shared_ref = await service.promote(personal, ref.identifier, owner_access, owner)

    assert any(
        s.reference.identifier == shared_ref.identifier
        for s in await service.list_documents(owner_access)
    )
    await service.revoke(shared_ref.identifier, owner_access)
    assert not any(
        s.reference.identifier == shared_ref.identifier
        for s in await service.list_documents(owner_access)
    )
