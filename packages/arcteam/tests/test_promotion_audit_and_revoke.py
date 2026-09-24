"""Durable promotion decisions and fleet revocation behavior."""

from __future__ import annotations

from pathlib import Path

import pytest
from arcmemory.adapters.personal_knowledge import PersonalKnowledgeAdapter
from arctrust import AgentIdentity
from arctrust.audit import AuditEvent

from arcteam.shared_knowledge import FleetSharedKnowledgeService


class _Access:
    def __init__(self, identity: AgentIdentity, clearance: str = "UNCLASSIFIED") -> None:
        self.caller_did = identity.did
        self.clearance = clearance


class _InsightDraft:
    title = "Close timing"
    content = "The month-end close runs on the third business day."
    classification = "UNCLASSIFIED"
    tags = ("insight",)
    document_type = "insight"


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def write_durable(self, event: AuditEvent) -> None:
        self.events.append(event)


class _FailingDecisionSink(_RecordingSink):
    def write_durable(self, event: AuditEvent) -> None:
        raise OSError("audit unavailable")


async def test_promotion_decision_emits_audit_with_did_item_score_and_decision(
    tmp_path: Path,
) -> None:
    """An auto-promotion emits a decision audit carrying origin DID + item id + score."""
    owner = AgentIdentity.generate("test", "owner")
    access = _Access(owner)
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    personal = PersonalKnowledgeAdapter(tmp_path / "owner", owner.did)
    sink = _RecordingSink()

    ref = await personal.save(_InsightDraft(), access)
    shared = await service.promote(
        personal,
        ref.identifier,
        access,
        owner,
        audit_sink=sink,
        decision="auto",
        effective_score=2,
    )

    decision_events = [e for e in sink.events if "promotion" in e.action]
    assert decision_events, "no per-decision promotion audit event was emitted"
    event = decision_events[0]
    assert event.actor_did == owner.did
    assert event.extra.get("decision") == "auto"
    assert int(event.extra.get("effective_score")) == 2
    assert str(event.extra.get("item_id")) == ref.identifier
    assert shared.scope == "shared"


async def test_failed_decision_audit_prevents_shared_write(tmp_path: Path) -> None:
    owner = AgentIdentity.generate("test", "owner")
    access = _Access(owner)
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    personal = PersonalKnowledgeAdapter(tmp_path / "owner", owner.did)
    ref = await personal.save(_InsightDraft(), access)

    with pytest.raises(OSError, match="audit unavailable"):
        await service.promote(
            personal, ref.identifier, access, owner,
            audit_sink=_FailingDecisionSink(), decision="auto", effective_score=2,
        )

    assert await service.list_documents(access) == []


async def test_invalid_promotion_decision_prevents_shared_write(tmp_path: Path) -> None:
    owner = AgentIdentity.generate("test", "owner")
    access = _Access(owner)
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    personal = PersonalKnowledgeAdapter(tmp_path / "owner", owner.did)
    ref = await personal.save(_InsightDraft(), access)
    sink = _RecordingSink()

    for decision, score in (("auto", 0), ("approved", 11), ("never", 2)):
        with pytest.raises(ValueError, match="invalid promotion decision"):
            await service.promote(
                personal, ref.identifier, access, owner,
                audit_sink=sink, decision=decision, effective_score=score,
            )

    assert await service.list_documents(access) == []


async def test_revoked_promotion_disappears_from_search_and_read(tmp_path: Path) -> None:
    """After revoke, the item is gone from both search and read (fail-closed)."""
    owner = AgentIdentity.generate("test", "owner")
    reader = AgentIdentity.generate("test", "reader")
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    owner_access = _Access(owner)
    reader_access = _Access(reader)
    personal = PersonalKnowledgeAdapter(tmp_path / "owner", owner.did)

    ref = await personal.save(_InsightDraft(), owner_access)
    shared = await service.promote(personal, ref.identifier, owner_access, owner)

    assert await service.search("close", reader_access), "item was not searchable before revoke"

    await service.revoke(shared.identifier, owner_access)

    assert await service.search("close", reader_access) == []
    with pytest.raises(FileNotFoundError):
        await service.read(shared.identifier, reader_access)
