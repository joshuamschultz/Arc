"""Durable classifier promotion decisions and fleet revocation (SPEC-083 T-1205, COMP-024).

``FleetSharedKnowledgeService.promote`` takes the classifier decision shape:
``decision="classifier_promote"``, ``confidence`` (0.90 <= x <= 1.0, a real
float), a non-empty ``classifier_version``, and a sink with ``write_durable``.
A durable ``knowledge.promotion_decision`` is written BEFORE the shared write;
``knowledge.promotion_completed`` follows it. The old score/band shape
(``decision in {"auto", "approved"}`` + ``effective_score``) is deleted.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest
from arcmemory.adapters.personal_knowledge import PersonalKnowledgeAdapter
from arctrust import AgentIdentity
from arctrust.audit import AuditEvent
from arctrust.identity import did_from_public_key
from arctrust.signer import InProcessSigner

from arcteam.shared_knowledge import FleetSharedKnowledgeService

VERSION = "jev-1.13.0"


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


class _EntityDraft:
    def __init__(self, content: str) -> None:
        self.title = "Acme"
        self.content = content
        self.classification = "UNCLASSIFIED"
        self.tags = ("entity",)
        self.document_type = "entity"


class _RecordingSink:
    """Records every event and whether a shared document existed at that moment."""

    def __init__(self, documents_dir: Path | None = None) -> None:
        self.events: list[AuditEvent] = []
        self.durable: list[AuditEvent] = []
        self.docs_present_at: dict[str, bool] = {}
        self._documents_dir = documents_dir

    def _snapshot(self, event: AuditEvent) -> None:
        if self._documents_dir is not None:
            present = self._documents_dir.exists() and any(self._documents_dir.glob("*.md"))
            self.docs_present_at.setdefault(event.action, present)

    def write(self, event: AuditEvent) -> None:
        self._snapshot(event)
        self.events.append(event)

    def write_durable(self, event: AuditEvent) -> None:
        self._snapshot(event)
        self.events.append(event)
        self.durable.append(event)


class _NonDurableSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _FailingDecisionSink(_RecordingSink):
    def write_durable(self, event: AuditEvent) -> None:
        raise OSError("audit unavailable")


async def _setup(tmp_path: Path) -> tuple[Any, ...]:
    owner = AgentIdentity.generate("test", "owner")
    access = _Access(owner)
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    personal = PersonalKnowledgeAdapter(tmp_path / "owner", owner.did)
    ref = await personal.save(_InsightDraft(), access)
    return owner, access, service, personal, ref


def _documents_dir(service: FleetSharedKnowledgeService) -> Path:
    return service.backend.root / "documents"


# ---------------------------------------------------------------------------
# Happy path — durable decision before write, completion after
# ---------------------------------------------------------------------------


async def test_classifier_promotion_writes_durable_decision_before_shared_write(
    tmp_path: Path,
) -> None:
    owner, access, service, personal, ref = await _setup(tmp_path)
    sink = _RecordingSink(_documents_dir(service))

    shared = await service.promote(
        personal,
        ref.identifier,
        access,
        owner,
        audit_sink=sink,
        decision="classifier_promote",
        confidence=0.97,
        classifier_version=VERSION,
    )

    decisions = [e for e in sink.durable if e.action == "knowledge.promotion_decision"]
    assert len(decisions) == 1, "no durable knowledge.promotion_decision event"
    assert sink.docs_present_at["knowledge.promotion_decision"] is False, (
        "decision audit must be durable BEFORE the shared document is written"
    )
    event = decisions[0]
    assert event.actor_did == owner.did
    assert event.outcome == "allow"
    assert event.extra.get("decision") == "classifier_promote"
    assert float(event.extra.get("confidence")) == pytest.approx(0.97)
    assert event.extra.get("classifier_version") == VERSION
    assert str(event.extra.get("item_id")) == ref.identifier
    assert "effective_score" not in event.extra
    assert shared.scope == "shared"


async def test_classifier_promotion_emits_completed_after_write(tmp_path: Path) -> None:
    owner, access, service, personal, ref = await _setup(tmp_path)
    sink = _RecordingSink(_documents_dir(service))

    shared = await service.promote(
        personal,
        ref.identifier,
        access,
        owner,
        audit_sink=sink,
        decision="classifier_promote",
        confidence=0.97,
        classifier_version=VERSION,
    )

    actions = [e.action for e in sink.events]
    assert "knowledge.promotion_completed" in actions
    assert actions.index("knowledge.promotion_decision") < actions.index(
        "knowledge.promotion_completed"
    )
    assert sink.docs_present_at["knowledge.promotion_completed"] is True
    completed = next(e for e in sink.events if e.action == "knowledge.promotion_completed")
    assert completed.target == shared.identifier
    assert completed.extra.get("decision") == "classifier_promote"
    assert float(completed.extra.get("confidence")) == pytest.approx(0.97)
    assert completed.extra.get("classifier_version") == VERSION
    assert str(completed.extra.get("item_id")) == ref.identifier
    assert "effective_score" not in completed.extra


@pytest.mark.parametrize("confidence", [0.90, 1.0])
async def test_confidence_boundaries_are_accepted(tmp_path: Path, confidence: float) -> None:
    """Narrowness: the inclusive bounds 0.90 and 1.0 must still promote."""
    owner, access, service, personal, ref = await _setup(tmp_path)

    await service.promote(
        personal,
        ref.identifier,
        access,
        owner,
        audit_sink=_RecordingSink(),
        decision="classifier_promote",
        confidence=confidence,
        classifier_version=VERSION,
    )

    assert len(await service.list_documents(access)) == 1


# ---------------------------------------------------------------------------
# Refusals — no write, no allow-decision audit
# ---------------------------------------------------------------------------


_INVALID = [
    pytest.param("auto", 0.97, VERSION, id="legacy-auto"),
    pytest.param("approved", 0.97, VERSION, id="legacy-approved"),
    pytest.param("never", 0.97, VERSION, id="never"),
    pytest.param("", 0.97, VERSION, id="empty-decision"),
    pytest.param("classifier_promote", 0.89, VERSION, id="below-floor"),
    pytest.param("classifier_promote", 0.8999, VERSION, id="just-below-floor"),
    pytest.param("classifier_promote", 1.01, VERSION, id="above-one"),
    pytest.param("classifier_promote", -0.97, VERSION, id="negative"),
    pytest.param("classifier_promote", math.nan, VERSION, id="nan"),
    pytest.param("classifier_promote", math.inf, VERSION, id="inf"),
    pytest.param("classifier_promote", True, VERSION, id="bool-confidence"),
    pytest.param("classifier_promote", "0.97", VERSION, id="string-confidence"),
    pytest.param("classifier_promote", None, VERSION, id="missing-confidence"),
    pytest.param("classifier_promote", 0.97, "", id="empty-version"),
    pytest.param("classifier_promote", 0.97, "   ", id="blank-version"),
    pytest.param("classifier_promote", 0.97, None, id="missing-version"),
]


@pytest.mark.parametrize(("decision", "confidence", "version"), _INVALID)
async def test_invalid_classifier_decision_is_refused_with_no_write(
    tmp_path: Path, decision: str, confidence: Any, version: Any
) -> None:
    owner, access, service, personal, ref = await _setup(tmp_path)
    sink = _RecordingSink()

    with pytest.raises(ValueError):
        await service.promote(
            personal,
            ref.identifier,
            access,
            owner,
            audit_sink=sink,
            decision=decision,
            confidence=confidence,
            classifier_version=version,
        )

    assert await service.list_documents(access) == []
    assert not any(
        e.action == "knowledge.promotion_decision" and e.outcome == "allow" for e in sink.events
    )


async def test_classifier_decision_without_durable_sink_is_refused(tmp_path: Path) -> None:
    owner, access, service, personal, ref = await _setup(tmp_path)

    for sink in (_NonDurableSink(), None):
        with pytest.raises(ValueError):
            await service.promote(
                personal,
                ref.identifier,
                access,
                owner,
                audit_sink=sink,
                decision="classifier_promote",
                confidence=0.97,
                classifier_version=VERSION,
            )

    assert await service.list_documents(access) == []


async def test_failed_decision_audit_prevents_shared_write(tmp_path: Path) -> None:
    owner, access, service, personal, ref = await _setup(tmp_path)

    with pytest.raises(OSError, match="audit unavailable"):
        await service.promote(
            personal,
            ref.identifier,
            access,
            owner,
            audit_sink=_FailingDecisionSink(),
            decision="classifier_promote",
            confidence=0.97,
            classifier_version=VERSION,
        )

    assert await service.list_documents(access) == []


async def test_legacy_effective_score_parameter_is_gone(tmp_path: Path) -> None:
    """No legacy shims: the score/band keyword no longer exists on promote()."""
    owner, access, service, personal, ref = await _setup(tmp_path)

    # The complete OLD call shape (only legacy kwargs) must no longer be accepted.
    with pytest.raises(TypeError):
        await service.promote(
            personal,
            ref.identifier,
            access,
            owner,
            audit_sink=_RecordingSink(),
            decision="auto",
            effective_score=2,
        )

    assert await service.list_documents(access) == []


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


async def test_revoked_classifier_promotion_disappears_from_search_and_read(
    tmp_path: Path,
) -> None:
    """After revoke, the item is gone from both search and read (fail-closed)."""
    owner, owner_access, service, personal, ref = await _setup(tmp_path)
    reader_access = _Access(AgentIdentity.generate("test", "reader"))

    shared = await service.promote(
        personal,
        ref.identifier,
        owner_access,
        owner,
        audit_sink=_RecordingSink(),
        decision="classifier_promote",
        confidence=0.97,
        classifier_version=VERSION,
    )
    assert await service.search("close", reader_access), "item was not searchable before revoke"

    await service.revoke(shared.identifier, owner_access)

    assert await service.search("close", reader_access) == []
    with pytest.raises(FileNotFoundError):
        await service.read(shared.identifier, reader_access)


# ---------------------------------------------------------------------------
# Operator demote — signed tombstone, no erasure (alpha-2 item 16)
# ---------------------------------------------------------------------------


def _operator_signer(seed: bytes = b"\x0a" * 32) -> InProcessSigner:
    return InProcessSigner(seed)


def _operator_did(signer: InProcessSigner) -> str:
    return did_from_public_key(signer.public_key, org="operator", agent_type="approver")


def _operator_service(tmp_path: Path, signer: InProcessSigner) -> FleetSharedKnowledgeService:
    return FleetSharedKnowledgeService.for_arc_team(
        tmp_path, operator_public_key=signer.public_key
    )


async def _shared_insight(tmp_path: Path, signer: InProcessSigner) -> tuple[Any, ...]:
    owner = AgentIdentity.generate("test", "owner")
    access = _Access(owner)
    service = _operator_service(tmp_path, signer)
    personal = PersonalKnowledgeAdapter(tmp_path / "owner", owner.did)
    ref = await personal.save(_InsightDraft(), access)
    shared = await service.promote(
        personal,
        ref.identifier,
        access,
        owner,
        audit_sink=_RecordingSink(),
        decision="classifier_promote",
        confidence=0.97,
        classifier_version=VERSION,
    )
    return owner, access, service, shared


async def test_operator_demote_writes_signed_tombstone_and_keeps_file(tmp_path: Path) -> None:
    operator = _operator_signer()
    owner, access, service, shared = await _shared_insight(tmp_path, operator)
    original = (service.backend.root / "documents" / f"{shared.identifier}.md").read_bytes()

    demotion = await service.demote(
        shared.identifier, operator_signer=operator, reason="stale", clearance="UNCLASSIFIED"
    )

    assert (demotion.identifier, demotion.demoted_by, demotion.reason) == (
        shared.identifier,
        _operator_did(operator),
        "stale",
    )
    tombstone = json.loads(
        (service.backend.root / "revocations" / f"{shared.identifier}.json").read_text()
    )
    assert tombstone["revoked_by"] == _operator_did(operator)
    assert tombstone["signature"]
    # No erasure: the signed bytes are kept, only moved out of the live set.
    kept = service.backend.root / "documents" / "revoked" / f"{shared.identifier}.md"
    assert kept.read_bytes() == original
    assert not (service.backend.root / "documents" / f"{shared.identifier}.md").exists()
    with pytest.raises(FileNotFoundError):
        await service.read(shared.identifier, access)
    assert await service.list_documents(access) == []
    assert await service.search("close", access) == []
    assert set(await service.demotions()) == {shared.identifier}
    audit = [json.loads(p.read_text()) for p in (service.backend.root / "audit").glob("*.json")]
    assert any(event["action"] == "knowledge.demoted" for event in audit)


async def test_demoted_document_listed_only_when_asked(tmp_path: Path) -> None:
    operator = _operator_signer()
    _, access, service, shared = await _shared_insight(tmp_path, operator)
    await service.demote(
        shared.identifier, operator_signer=operator, reason="stale", clearance="UNCLASSIFIED"
    )

    listed = await service.list_documents(access, include_demoted=True)

    assert [(s.reference.identifier, s.demotion is not None) for s in listed] == [
        (shared.identifier, True)
    ]
    assert listed[0].kind == "insight"


async def test_entity_demote_moves_block_to_revoked_dir(tmp_path: Path) -> None:
    operator = _operator_signer()
    service = _operator_service(tmp_path, operator)
    agent_a = AgentIdentity.generate("test", "agent-a")
    agent_b = AgentIdentity.generate("test", "agent-b")
    for agent, fact in ((agent_a, "role: vendor"), (agent_b, "role: partner")):
        personal = PersonalKnowledgeAdapter(tmp_path / agent.did.rsplit("/", 1)[-1], agent.did)
        ref = await personal.save(_EntityDraft(fact), _Access(agent))
        shared = await service.promote(personal, ref.identifier, _Access(agent), agent)
    entity_dir = service.backend.root / "entities" / shared.identifier
    live = {path.name: path.read_bytes() for path in entity_dir.glob("*.md")}
    assert len(live) == 2

    await service.demote(
        shared.identifier,
        operator_signer=operator,
        reason="wrong entity",
        clearance="UNCLASSIFIED",
    )

    assert list(entity_dir.glob("*.md")) == []
    kept = sorted((entity_dir / "revoked").glob("*.md"))
    assert sorted(path.read_bytes() for path in kept) == sorted(live.values())
    with pytest.raises(FileNotFoundError):
        await service.read(shared.identifier, _Access(agent_a))
    # A demoted entity is never re-promoted into, by any contributor.
    personal = PersonalKnowledgeAdapter(tmp_path / "late", agent_a.did)
    ref = await personal.save(_EntityDraft("role: supplier"), _Access(agent_a))
    with pytest.raises(PermissionError, match="demoted"):
        await service.promote(personal, ref.identifier, _Access(agent_a), agent_a)


async def test_entity_contributor_revoke_keeps_its_signed_block(tmp_path: Path) -> None:
    """No erasure: a contributor's own revoke moves its block aside, never unlinks it."""
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    agent = AgentIdentity.generate("test", "agent-a")
    personal = PersonalKnowledgeAdapter(tmp_path / "a", agent.did)
    ref = await personal.save(_EntityDraft("role: vendor"), _Access(agent))
    shared = await service.promote(personal, ref.identifier, _Access(agent), agent)
    entity_dir = service.backend.root / "entities" / shared.identifier
    [block] = list(entity_dir.glob("*.md"))
    original = block.read_bytes()

    await service.revoke(shared.identifier, _Access(agent))

    assert not block.exists()
    assert [path.read_bytes() for path in (entity_dir / "revoked").glob("*.md")] == [original]


async def test_non_operator_non_owner_refused(tmp_path: Path) -> None:
    operator = _operator_signer()
    _, access, service, shared = await _shared_insight(tmp_path, operator)
    stranger = _operator_signer(b"\x0b" * 32)

    with pytest.raises(PermissionError):
        await service.demote(
            shared.identifier,
            operator_signer=stranger,
            reason="mine now",
            clearance="UNCLASSIFIED",
        )
    with pytest.raises(PermissionError):
        await service.revoke(shared.identifier, _Access(AgentIdentity.generate("test", "x")))

    assert await service.demotions() == {}
    assert len(await service.list_documents(access)) == 1


async def test_demote_without_an_operator_trust_anchor_is_refused(tmp_path: Path) -> None:
    operator = _operator_signer()
    _, access, _, shared = await _shared_insight(tmp_path, operator)
    unanchored = FleetSharedKnowledgeService.for_arc_team(tmp_path)

    with pytest.raises(PermissionError):
        await unanchored.demote(
            shared.identifier, operator_signer=operator, reason="x", clearance="UNCLASSIFIED"
        )

    assert len(await unanchored.list_documents(access)) == 1


# ---------------------------------------------------------------------------
# Operator promote decision + provenance sidecar (alpha-2 item 16)
# ---------------------------------------------------------------------------

_DECIDER = "did:arc:operator:approver/abcd1234"


async def test_operator_promote_records_decider_and_needs_no_confidence(tmp_path: Path) -> None:
    owner, access, service, personal, ref = await _setup(tmp_path)
    sink = _RecordingSink(_documents_dir(service))

    shared = await service.promote(
        personal,
        ref.identifier,
        access,
        owner,
        audit_sink=sink,
        decision="operator_promote",
        decided_by=_DECIDER,
    )

    [decision] = [e for e in sink.durable if e.action == "knowledge.promotion_decision"]
    assert decision.extra["decision"] == "operator_promote"
    assert decision.extra["decided_by"] == _DECIDER
    assert sink.docs_present_at["knowledge.promotion_decision"] is False
    [provenance] = await service.provenance(shared.identifier, access)
    assert (provenance.decision, provenance.decided_by, provenance.contributor_did) == (
        "operator_promote",
        _DECIDER,
        owner.did,
    )
    assert provenance.source_ref == ref.identifier
    assert provenance.promoted_at


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"decided_by": None}, id="no-decider"),
        pytest.param({"decided_by": "operator"}, id="not-a-did"),
        pytest.param({"decided_by": _DECIDER, "confidence": 0.99}, id="smuggled-confidence"),
        pytest.param(
            {"decided_by": _DECIDER, "classifier_version": VERSION}, id="smuggled-classifier"
        ),
    ],
)
async def test_malformed_operator_promote_is_refused(
    tmp_path: Path, kwargs: dict[str, Any]
) -> None:
    owner, access, service, personal, ref = await _setup(tmp_path)

    with pytest.raises(ValueError):
        await service.promote(
            personal,
            ref.identifier,
            access,
            owner,
            audit_sink=_RecordingSink(),
            decision="operator_promote",
            **kwargs,
        )

    assert await service.list_documents(access) == []


async def test_classifier_promote_cannot_name_an_operator(tmp_path: Path) -> None:
    owner, access, service, personal, ref = await _setup(tmp_path)

    with pytest.raises(ValueError):
        await service.promote(
            personal,
            ref.identifier,
            access,
            owner,
            audit_sink=_RecordingSink(),
            decision="classifier_promote",
            confidence=0.97,
            classifier_version=VERSION,
            decided_by=_DECIDER,
        )


async def test_listing_carries_kind_contributors_and_promoted_at(tmp_path: Path) -> None:
    owner, access, service, personal, ref = await _setup(tmp_path)
    await service.promote(
        personal,
        ref.identifier,
        access,
        owner,
        audit_sink=_RecordingSink(),
        decision="classifier_promote",
        confidence=0.97,
        classifier_version=VERSION,
    )

    [summary] = await service.list_documents(access)

    assert summary.kind == "insight"
    assert summary.contributors == (owner.did,)
    assert summary.owner_did == owner.did
    assert summary.promoted_at is not None
    assert summary.demotion is None
