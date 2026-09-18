"""T-1132 (SPEC-083 COMP-008) — per-decision promotion audit + revoke visibility.

RED intent, two parts:

(a) PER-DECISION AUDIT (the failing driver). When a promotion decision is made, the
    system must emit an audit event carrying the ORIGIN DID, the ITEM id, the
    EFFECTIVE SCORE, and the DECISION (auto/queued/approved/denied/never) — REQ-445.
    Today the shared write path audits ``knowledge.saved`` /
    ``knowledge.collection_saved`` with only ``{action, identifier, actor_did,
    digest}``; neither the effective score nor the decision is recorded, and there
    is no audit of the promotion *decision* as such.

    Intended contract (for the GREEN implementer, T-1133 in
    ``arcteam/shared_knowledge/service.py``): ``promote`` accepts the routed
    ``decision`` and ``effective_score`` and emits a per-decision audit event
    through the shared audit emission point whose ``actor_did`` is the origin DID
    and whose ``extra`` carries ``item_id``, ``effective_score``, and ``decision``.

(b) REVOKE VISIBILITY. A promoted item is revocable through the shared store's
    ``revoke`` and disappears from BOTH ``search`` and ``read`` afterward (REQ-443).
    This part likely already holds — ``_search`` skips a revoked document because
    ``_read`` raises ``FileNotFoundError`` on it — so it is pinned here as a
    standing guarantee alongside the new audit requirement.
"""

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
    assert str(event.extra.get("item_id")) == shared.identifier


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
