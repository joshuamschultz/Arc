"""Canonical multi-contributor entity merge in shared knowledge (SPEC-083 T-1207, COMP-007/008).

Two agents promoting the same real-world entity (slug-keyed on its title)
produce ONE canonical shared entity. Each contributor owns a signed provenance
block (contributor DID + source digest + that contributor's content), so a
conflicting fact keeps both values, attributed. Re-promoting identical bytes is
a no-op; changed bytes replace only that contributor's block; a contributor's
revoke removes only its block; a block signed by a key that does not match the
contributor DID is refused.

Assumed interface (the contract these tests pin):

- ``FleetSharedKnowledgeService.promote(personal, ref, access, signer)`` on an
  ``entity`` document returns a ``SharedKnowledgeReference`` whose
  ``identifier`` is the SAME for every contributor of that entity.
- ``service.read(identifier, access)`` returns a document that also carries
  ``contributions``: a sequence of objects with ``contributor_did: str``,
  ``source_digest: str`` and ``content: str`` — one per live contributor.
- ``service.revoke(identifier, access)`` by a contributor removes only that
  contributor's block; the entity disappears once no block remains; a caller
  who is not a contributor is refused with ``PermissionError``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcmemory.adapters.personal_knowledge import PersonalKnowledgeAdapter
from arctrust import AgentIdentity

from arcteam.shared_knowledge import FleetSharedKnowledgeService


class _Access:
    def __init__(self, identity: AgentIdentity, clearance: str = "UNCLASSIFIED") -> None:
        self.caller_did = identity.did
        self.clearance = clearance


class _EntityDraft:
    """A promotable ENTITY document — same real-world entity, differing fact."""

    def __init__(self, content: str, title: str = "Acme") -> None:
        self.title = title
        self.content = content
        self.classification = "UNCLASSIFIED"
        self.tags = ("entity", "vendor")
        self.document_type = "entity"


def _personal(tmp_path: Path, identity: AgentIdentity) -> PersonalKnowledgeAdapter:
    return PersonalKnowledgeAdapter(
        tmp_path / "personal" / identity.did.replace(":", "_").replace("/", "_"), identity.did
    )


async def _promote_entity(
    service: FleetSharedKnowledgeService,
    tmp_path: Path,
    identity: AgentIdentity,
    content: str,
    *,
    title: str = "Acme",
    signer: AgentIdentity | None = None,
) -> tuple[Any, str]:
    """Save an entity privately, promote it; return (shared ref, source digest)."""
    access = _Access(identity)
    personal = _personal(tmp_path, identity)
    ref = await personal.save(_EntityDraft(content, title), access)
    source = await personal.export_for_promotion(ref.identifier, access)
    shared = await service.promote(personal, ref.identifier, access, signer or identity)
    return shared, str(source.digest)


def _contributions(document: Any) -> dict[str, Any]:
    """Map contributor DID -> contribution block of a read canonical entity."""
    blocks = list(document.contributions)
    by_did = {block.contributor_did: block for block in blocks}
    assert len(by_did) == len(blocks), "a contributor appears in more than one block"
    return by_did


def _store_snapshot(service: FleetSharedKnowledgeService) -> dict[str, bytes]:
    """Every persisted byte of the shared store except the append-only audit trail."""
    root = service.backend.root
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and "audit" not in path.relative_to(root).parts
        and not path.name.startswith(".")
    }


@pytest.fixture
def agents() -> tuple[AgentIdentity, AgentIdentity, AgentIdentity]:
    return (
        AgentIdentity.generate("test", "agent-a"),
        AgentIdentity.generate("test", "agent-b"),
        AgentIdentity.generate("test", "reader"),
    )


# ---------------------------------------------------------------------------
# One canonical entity from two contributors
# ---------------------------------------------------------------------------


async def test_two_agents_promoting_same_entity_produce_one_canonical_entity(
    tmp_path: Path, agents: tuple[AgentIdentity, AgentIdentity, AgentIdentity]
) -> None:
    agent_a, agent_b, reader = agents
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)

    shared_a, _ = await _promote_entity(
        service, tmp_path, agent_a, "Acme\nrole: vendor\nlead_time: 2 weeks"
    )
    shared_b, _ = await _promote_entity(
        service, tmp_path, agent_b, "Acme\nrole: vendor\nlead_time: 2 weeks"
    )

    assert shared_a.scope == shared_b.scope == "shared"
    assert shared_a.identifier == shared_b.identifier, "second contributor forked a duplicate"
    documents = await service.list_documents(_Access(reader))
    assert [d.title for d in documents] == ["Acme"]
    assert len(await service.search("Acme", _Access(reader))) == 1


async def test_different_entities_stay_separate(
    tmp_path: Path, agents: tuple[AgentIdentity, AgentIdentity, AgentIdentity]
) -> None:
    """Narrowness: merge is keyed on the entity, not on 'any entity'."""
    agent_a, agent_b, reader = agents
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)

    shared_a, _ = await _promote_entity(service, tmp_path, agent_a, "Acme\nrole: vendor")
    shared_b, _ = await _promote_entity(
        service, tmp_path, agent_b, "Globex\nrole: partner", title="Globex"
    )

    assert shared_a.identifier != shared_b.identifier
    titles = sorted(d.title for d in await service.list_documents(_Access(reader)))
    assert titles == ["Acme", "Globex"]


# ---------------------------------------------------------------------------
# Conflicting facts keep both values with provenance
# ---------------------------------------------------------------------------


async def test_conflicting_fact_keeps_both_values_with_contributor_did_and_digest(
    tmp_path: Path, agents: tuple[AgentIdentity, AgentIdentity, AgentIdentity]
) -> None:
    agent_a, agent_b, reader = agents
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)

    shared, digest_a = await _promote_entity(service, tmp_path, agent_a, "Acme\nrole: vendor")
    _, digest_b = await _promote_entity(service, tmp_path, agent_b, "Acme\nrole: partner")

    document = await service.read(shared.identifier, _Access(reader))
    blocks = _contributions(document)
    assert set(blocks) == {agent_a.did, agent_b.did}
    assert blocks[agent_a.did].source_digest == digest_a
    assert "role: vendor" in blocks[agent_a.did].content
    assert "role: partner" not in blocks[agent_a.did].content
    assert blocks[agent_b.did].source_digest == digest_b
    assert "role: partner" in blocks[agent_b.did].content
    assert "role: vendor" not in blocks[agent_b.did].content


# ---------------------------------------------------------------------------
# Idempotency and per-contributor update
# ---------------------------------------------------------------------------


async def test_repromoting_identical_entity_bytes_is_a_noop(
    tmp_path: Path, agents: tuple[AgentIdentity, AgentIdentity, AgentIdentity]
) -> None:
    agent_a, agent_b, _ = agents
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    first, _ = await _promote_entity(service, tmp_path, agent_a, "Acme\nrole: vendor")
    await _promote_entity(service, tmp_path, agent_b, "Acme\nrole: partner")
    before = _store_snapshot(service)

    again, _ = await _promote_entity(service, tmp_path, agent_a, "Acme\nrole: vendor")

    assert again.identifier == first.identifier
    assert _store_snapshot(service) == before, "identical re-promotion rewrote the store"


async def test_changed_entity_bytes_update_only_that_contributors_block(
    tmp_path: Path, agents: tuple[AgentIdentity, AgentIdentity, AgentIdentity]
) -> None:
    agent_a, agent_b, reader = agents
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    shared, _ = await _promote_entity(service, tmp_path, agent_a, "Acme\nrole: vendor")
    _, digest_b = await _promote_entity(service, tmp_path, agent_b, "Acme\nrole: partner")
    before_b = _contributions(await service.read(shared.identifier, _Access(reader)))[agent_b.did]

    updated, new_digest_a = await _promote_entity(
        service, tmp_path, agent_a, "Acme\nrole: supplier"
    )

    assert updated.identifier == shared.identifier
    blocks = _contributions(await service.read(shared.identifier, _Access(reader)))
    assert set(blocks) == {agent_a.did, agent_b.did}
    assert blocks[agent_a.did].source_digest == new_digest_a
    assert "role: supplier" in blocks[agent_a.did].content
    assert "role: vendor" not in blocks[agent_a.did].content
    assert blocks[agent_b.did].source_digest == digest_b == before_b.source_digest
    assert blocks[agent_b.did].content == before_b.content


# ---------------------------------------------------------------------------
# Revocation removes only the revoking contributor's block
# ---------------------------------------------------------------------------


async def test_one_contributors_revoke_removes_only_its_block(
    tmp_path: Path, agents: tuple[AgentIdentity, AgentIdentity, AgentIdentity]
) -> None:
    agent_a, agent_b, reader = agents
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    shared, _ = await _promote_entity(service, tmp_path, agent_a, "Acme\nrole: vendor")
    _, digest_b = await _promote_entity(service, tmp_path, agent_b, "Acme\nrole: partner")

    await service.revoke(shared.identifier, _Access(agent_a))

    document = await service.read(shared.identifier, _Access(reader))
    blocks = _contributions(document)
    assert set(blocks) == {agent_b.did}
    assert blocks[agent_b.did].source_digest == digest_b
    assert "role: vendor" not in document.content
    assert len(await service.search("Acme", _Access(reader))) == 1
    assert await service.search("vendor", _Access(reader)) == []


async def test_entity_disappears_after_last_contributor_revokes(
    tmp_path: Path, agents: tuple[AgentIdentity, AgentIdentity, AgentIdentity]
) -> None:
    agent_a, agent_b, reader = agents
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    shared, _ = await _promote_entity(service, tmp_path, agent_a, "Acme\nrole: vendor")
    await _promote_entity(service, tmp_path, agent_b, "Acme\nrole: partner")

    await service.revoke(shared.identifier, _Access(agent_a))
    await service.revoke(shared.identifier, _Access(agent_b))

    assert await service.search("Acme", _Access(reader)) == []
    with pytest.raises(FileNotFoundError):
        await service.read(shared.identifier, _Access(reader))


async def test_non_contributor_cannot_revoke_any_block(
    tmp_path: Path, agents: tuple[AgentIdentity, AgentIdentity, AgentIdentity]
) -> None:
    agent_a, agent_b, reader = agents
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    shared, _ = await _promote_entity(service, tmp_path, agent_a, "Acme\nrole: vendor")
    await _promote_entity(service, tmp_path, agent_b, "Acme\nrole: partner")
    before = _store_snapshot(service)

    with pytest.raises(PermissionError):
        await service.revoke(shared.identifier, _Access(reader))

    assert _store_snapshot(service) == before
    blocks = _contributions(await service.read(shared.identifier, _Access(reader)))
    assert set(blocks) == {agent_a.did, agent_b.did}


# ---------------------------------------------------------------------------
# Signature binding — a block must be signed by its contributor's key
# ---------------------------------------------------------------------------


async def test_block_signed_by_non_matching_key_is_rejected(
    tmp_path: Path, agents: tuple[AgentIdentity, AgentIdentity, AgentIdentity]
) -> None:
    """Agent A presents its own DID but signs with agent B's key: refused, and
    an existing canonical entity is left byte-identical."""
    agent_a, agent_b, reader = agents
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    shared, digest_b = await _promote_entity(service, tmp_path, agent_b, "Acme\nrole: partner")
    before = _store_snapshot(service)

    with pytest.raises(PermissionError):
        await _promote_entity(service, tmp_path, agent_a, "Acme\nrole: vendor", signer=agent_b)

    assert _store_snapshot(service) == before
    blocks = _contributions(await service.read(shared.identifier, _Access(reader)))
    assert set(blocks) == {agent_b.did}
    assert blocks[agent_b.did].source_digest == digest_b


async def test_wrong_key_first_contributor_creates_no_entity(
    tmp_path: Path, agents: tuple[AgentIdentity, AgentIdentity, AgentIdentity]
) -> None:
    agent_a, agent_b, reader = agents
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)

    with pytest.raises(PermissionError):
        await _promote_entity(service, tmp_path, agent_a, "Acme\nrole: vendor", signer=agent_b)

    assert await service.list_documents(_Access(reader)) == []


async def test_tampered_contributor_block_is_never_served(
    tmp_path: Path, agents: tuple[AgentIdentity, AgentIdentity, AgentIdentity]
) -> None:
    """An attacker with file access rewrites B's value on disk. The forged value
    must never reach a reader: the read fails closed or drops the bad block."""
    agent_a, agent_b, reader = agents
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)
    shared, _ = await _promote_entity(service, tmp_path, agent_a, "Acme\nrole: vendor")
    await _promote_entity(service, tmp_path, agent_b, "Acme\nrole: partner")
    tampered = 0
    for path in service.backend.root.rglob("*"):
        if path.is_file() and b"partner" in path.read_bytes():
            path.write_bytes(path.read_bytes().replace(b"partner", b"rival"))
            tampered += 1
    assert tampered, "test setup: B's value was not found on disk"

    try:
        document = await service.read(shared.identifier, _Access(reader))
    except (PermissionError, ValueError, FileNotFoundError):
        return
    served = document.content + "".join(b.content for b in document.contributions)
    assert "rival" not in served
    assert agent_b.did not in _contributions(document)
