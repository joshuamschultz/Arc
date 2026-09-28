"""SPEC-083 T-1212 — memory promotion abuse battery (shared-store side).

The arcmemory-side cases live in
``packages/arcmemory/tests/security/test_memory_promotion_abuse.py``. These are
the attacks that land on the fleet's shared store:

* Abuse 5 — forged origin DID: a draft whose owner DID is not the caller, or
  whose signer key does not match the claimed DID, is refused by
  ``FleetSharedKnowledgeBackend._authorize_write`` / signature binding, and the
  store is left byte-identical.
* A validly signed shared entity block copied into another entity's folder, or
  into another contributor's slot, is refused on read (``PermissionError``) —
  never re-attributed.
* A refused promote leaves no ``allow`` decision record behind (and no
  ``promotion_completed``): the durable decision is written only after every
  pre-write check passes.

Real objects throughout: ``FleetSharedKnowledgeService`` / backend on a tmp
root, arcmemory ``PersonalKnowledgeAdapter`` and ``SharedKnowledgeAdapter``,
real ``AgentIdentity`` keys. Faked only: the audit sink (a durable recorder).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from arcmemory.adapters.personal_knowledge import PersonalKnowledgeAdapter
from arcmemory.adapters.shared_knowledge import SharedKnowledgeAdapter
from arctrust import AgentIdentity
from arctrust.audit import AuditEvent

from arcteam.shared_knowledge import FleetSharedKnowledgeService

_VERSION = "jev-1.13.0"


class _Access:
    def __init__(self, identity: AgentIdentity, clearance: str = "UNCLASSIFIED") -> None:
        self.caller_did = identity.did
        self.clearance = clearance


class _Draft:
    def __init__(
        self,
        content: str,
        *,
        title: str = "Close timing",
        document_type: str = "insight",
        classification: str = "UNCLASSIFIED",
    ) -> None:
        self.title = title
        self.content = content
        self.classification = classification
        self.tags: tuple[str, ...] = (document_type,)
        self.document_type = document_type


class _DurableSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def write_durable(self, event: AuditEvent) -> None:
        self.events.append(event)

    def allow_decisions(self) -> list[AuditEvent]:
        return [
            event
            for event in self.events
            if event.action in {"knowledge.promotion_decision", "knowledge.promotion_completed"}
            and event.outcome == "allow"
        ]


@pytest.fixture
def agents() -> tuple[AgentIdentity, AgentIdentity, AgentIdentity]:
    return (
        AgentIdentity.generate("test", "agent-a"),
        AgentIdentity.generate("test", "agent-b"),
        AgentIdentity.generate("test", "reader"),
    )


def _personal(tmp_path: Path, identity: AgentIdentity) -> PersonalKnowledgeAdapter:
    safe = identity.did.replace(":", "_").replace("/", "_")
    return PersonalKnowledgeAdapter(tmp_path / "personal" / safe, identity.did)


def _backend(service: FleetSharedKnowledgeService) -> Any:
    """The fleet backend as the arcmemory adapter's structural seam (as the service does)."""
    return service.backend


def _snapshot(service: FleetSharedKnowledgeService) -> dict[str, bytes]:
    """Every persisted byte of the shared store except the append-only audit trail."""
    root = service.backend.root
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and "audit" not in path.relative_to(root).parts
        and not path.name.startswith(".")
    }


async def _promote(
    service: FleetSharedKnowledgeService,
    tmp_path: Path,
    owner: AgentIdentity,
    draft: _Draft,
    *,
    access: _Access | None = None,
    signer: AgentIdentity | None = None,
    sink: _DurableSink | None = None,
) -> Any:
    """Save ``draft`` privately as ``owner`` and promote it as a classifier decision."""
    owner_access = _Access(owner)
    personal = _personal(tmp_path, owner)
    ref = await personal.save(draft, owner_access)
    return await service.promote(
        personal,
        ref.identifier,
        access or owner_access,
        signer or owner,
        audit_sink=sink or _DurableSink(),
        decision="classifier_promote",
        confidence=0.97,
        classifier_version=_VERSION,
    )


# ============================================================================
# Abuse 5 — forged origin DID at the shared backend
# ============================================================================


async def test_backend_refuses_a_draft_owned_by_another_did(
    tmp_path: Path, agents: tuple[AgentIdentity, AgentIdentity, AgentIdentity]
) -> None:
    """Agent A submits a draft validly signed as agent B: owner != caller."""
    agent_a, agent_b, _ = agents
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    as_b = SharedKnowledgeAdapter(_backend(service), owner_did=agent_b.did, signer=agent_b)

    with pytest.raises(PermissionError, match="owner does not match caller"):
        await as_b.save(_Draft("Acme pays net-60."), _Access(agent_a))

    assert _snapshot(service) == {}


async def test_backend_refuses_a_did_claim_signed_by_another_key(
    tmp_path: Path, agents: tuple[AgentIdentity, AgentIdentity, AgentIdentity]
) -> None:
    """Agent A claims its own DID but signs with B's key: signer not bound to the DID."""
    agent_a, agent_b, _ = agents
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    forged = SharedKnowledgeAdapter(_backend(service), owner_did=agent_a.did, signer=agent_b)

    with pytest.raises(PermissionError, match="signer does not match owner DID"):
        await forged.save(_Draft("Acme pays net-60."), _Access(agent_a))

    assert _snapshot(service) == {}


async def test_backend_accepts_the_matching_owner_signer_and_caller(
    tmp_path: Path, agents: tuple[AgentIdentity, AgentIdentity, AgentIdentity]
) -> None:
    """Narrowness pair: the genuine owner still writes."""
    agent_a, _, _ = agents
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    genuine = SharedKnowledgeAdapter(_backend(service), owner_did=agent_a.did, signer=agent_a)

    reference = await genuine.save(_Draft("Acme pays net-60."), _Access(agent_a))

    assert reference.scope == "shared"


# ============================================================================
# Shared entity blocks cannot be moved between entities or contributors
# ============================================================================


async def _two_entities(
    tmp_path: Path, agents: tuple[AgentIdentity, AgentIdentity, AgentIdentity]
) -> tuple[FleetSharedKnowledgeService, str, str]:
    agent_a, agent_b, _ = agents
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    acme = await _promote(
        service, tmp_path, agent_a, _Draft("role: vendor", title="Acme", document_type="entity")
    )
    await _promote(
        service, tmp_path, agent_b, _Draft("role: partner", title="Acme", document_type="entity")
    )
    globex = await _promote(
        service,
        tmp_path,
        agent_b,
        _Draft("role: prospect", title="Globex", document_type="entity"),
    )
    return service, acme.identifier, globex.identifier


def _entity_dir(service: FleetSharedKnowledgeService, identifier: str) -> Path:
    return service.backend.root / "entities" / identifier


def _block_of(service: FleetSharedKnowledgeService, identifier: str, marker: bytes) -> Path:
    matches = [
        p for p in _entity_dir(service, identifier).glob("*.md") if marker in p.read_bytes()
    ]
    assert len(matches) == 1, f"test setup: expected one block containing {marker!r}"
    return matches[0]


async def test_entity_block_copied_into_another_entity_is_refused(
    tmp_path: Path, agents: tuple[AgentIdentity, AgentIdentity, AgentIdentity]
) -> None:
    """A's valid Acme block dropped into Globex's folder must not make A a Globex contributor."""
    _, _, reader = agents
    service, acme, globex = await _two_entities(tmp_path, agents)
    await service.read(globex, _Access(reader))  # untampered read works
    a_block = _block_of(service, acme, b"vendor")
    shutil.copy2(a_block, _entity_dir(service, globex) / a_block.name)

    with pytest.raises(PermissionError, match="not bound to its entity and contributor"):
        await service.read(globex, _Access(reader))


async def test_entity_block_copied_into_another_contributors_slot_is_refused(
    tmp_path: Path, agents: tuple[AgentIdentity, AgentIdentity, AgentIdentity]
) -> None:
    """A's valid Acme block written over B's slot must not be served as B's value."""
    _, _, reader = agents
    service, acme, _ = await _two_entities(tmp_path, agents)
    a_block = _block_of(service, acme, b"vendor")
    b_slot = _block_of(service, acme, b"partner")
    b_slot.write_bytes(a_block.read_bytes())

    with pytest.raises(PermissionError, match="not bound to its entity and contributor"):
        await service.read(acme, _Access(reader))


async def test_entity_block_under_a_fresh_slot_name_is_refused(
    tmp_path: Path, agents: tuple[AgentIdentity, AgentIdentity, AgentIdentity]
) -> None:
    """A duplicate of A's block under an invented slot name is not a second contributor."""
    _, _, reader = agents
    service, acme, _ = await _two_entities(tmp_path, agents)
    a_block = _block_of(service, acme, b"vendor")
    shutil.copy2(a_block, _entity_dir(service, acme) / "0123456789abcdef.md")

    with pytest.raises(PermissionError, match="not bound to its entity and contributor"):
        await service.read(acme, _Access(reader))


# ============================================================================
# A refused promote leaves no ``allow`` decision record
# ============================================================================


async def test_refused_promote_signed_by_another_key_records_no_allow(
    tmp_path: Path, agents: tuple[AgentIdentity, AgentIdentity, AgentIdentity]
) -> None:
    agent_a, agent_b, _ = agents
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    sink = _DurableSink()

    with pytest.raises(PermissionError):
        await _promote(
            service, tmp_path, agent_a, _Draft("Acme pays net-60."), signer=agent_b, sink=sink
        )

    assert sink.allow_decisions() == []
    assert _snapshot(service) == {}


async def test_refused_promote_of_another_agents_personal_note_records_no_allow(
    tmp_path: Path, agents: tuple[AgentIdentity, AgentIdentity, AgentIdentity]
) -> None:
    """A presents its own access against B's personal store: export refuses."""
    agent_a, agent_b, _ = agents
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    sink = _DurableSink()

    with pytest.raises(PermissionError):
        await _promote(
            service,
            tmp_path,
            agent_b,
            _Draft("Acme pays net-60."),
            access=_Access(agent_a),
            signer=agent_a,
            sink=sink,
        )

    assert sink.allow_decisions() == []
    assert _snapshot(service) == {}


async def test_refused_write_down_promote_records_no_allow(
    tmp_path: Path, agents: tuple[AgentIdentity, AgentIdentity, AgentIdentity]
) -> None:
    """A SECRET-cleared caller cannot launder an UNCLASSIFIED label into the store."""
    agent_a, _, _ = agents
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    sink = _DurableSink()

    with pytest.raises(PermissionError):
        await _promote(
            service,
            tmp_path,
            agent_a,
            _Draft("Acme pays net-60."),
            access=_Access(agent_a, clearance="SECRET"),
            sink=sink,
        )

    assert sink.allow_decisions() == []
    assert _snapshot(service) == {}


async def test_refused_non_promotable_type_records_no_allow(
    tmp_path: Path, agents: tuple[AgentIdentity, AgentIdentity, AgentIdentity]
) -> None:
    agent_a, _, _ = agents
    service = FleetSharedKnowledgeService.for_arc_team(
        tmp_path, promotable_document_types={"entity"}
    )
    sink = _DurableSink()

    with pytest.raises(PermissionError):
        await _promote(service, tmp_path, agent_a, _Draft("Acme pays net-60."), sink=sink)

    assert sink.allow_decisions() == []
    assert _snapshot(service) == {}


async def test_accepted_promote_records_exactly_one_allow_decision(
    tmp_path: Path, agents: tuple[AgentIdentity, AgentIdentity, AgentIdentity]
) -> None:
    """Narrowness pair: a genuine promote records its decision (and completion)."""
    agent_a, _, _ = agents
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    sink = _DurableSink()

    await _promote(service, tmp_path, agent_a, _Draft("Acme pays net-60."), sink=sink)

    assert sorted(e.action for e in sink.allow_decisions()) == [
        "knowledge.promotion_completed",
        "knowledge.promotion_decision",
    ]
