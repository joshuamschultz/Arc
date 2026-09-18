"""T-1130 (SPEC-083 COMP-007) — promoting an ENTITY merges into one canonical.

RED intent. Today ``FleetSharedKnowledgeService.promote`` writes ONE new signed
document per promotion, addressed by ``sha256(owner_did, title, digest)[:16]``
(see ``FleetSharedKnowledgeBackend._identifier``). So when two agents each promote
the SAME real-world entity (e.g. the vendor "Acme"), the shared store ends up with
TWO documents — a duplicate canonical entity, not a merge.

REQ-441 / REQ-442 require instead:

1. Promoting an entity that already exists in the shared store MERGES into the one
   canonical shared entity — no duplicate document.
2. A CONFLICTING field keeps BOTH agents' values with per-agent provenance (the
   contributing DID travels with the value), never a silent last-writer overwrite.
3. Re-promoting the same entity is idempotent — it updates provenance only and
   never adds a second canonical document.

Intended contract (for the GREEN implementer, T-1131 in
``arcteam/shared_knowledge/backend.py``): an ``entity``-typed promotion is keyed by
the entity's stable identity (its name/slug), not by ``(owner, title, digest)``, so
a second contributor lands on the SAME canonical document; conflicting facts are
retained with the contributing DID as provenance; and an unchanged re-promotion is
a no-op on the document count.

Observable assertions below are deliberately representation-agnostic: they count
canonical entity documents and read the merged content, not internal merge shapes.
"""

from __future__ import annotations

from pathlib import Path

from arcmemory.adapters.personal_knowledge import PersonalKnowledgeAdapter
from arctrust import AgentIdentity

from arcteam.shared_knowledge import FleetSharedKnowledgeService


class _Access:
    def __init__(self, identity: AgentIdentity, clearance: str = "UNCLASSIFIED") -> None:
        self.caller_did = identity.did
        self.clearance = clearance


class _EntityDraft:
    """A promotable ENTITY document — same real-world entity, differing fact."""

    def __init__(self, content: str) -> None:
        self.title = "Acme"
        self.content = content
        self.classification = "UNCLASSIFIED"
        self.tags = ("entity", "vendor")
        self.document_type = "entity"


def _entity_docs(service: FleetSharedKnowledgeService) -> list[Path]:
    return sorted((service.backend.root / "documents").glob("*.md"))


async def _promote_entity(
    service: FleetSharedKnowledgeService,
    tmp_path: Path,
    identity: AgentIdentity,
    content: str,
) -> object:
    access = _Access(identity)
    personal = PersonalKnowledgeAdapter(tmp_path / identity.did.replace("/", "_"), identity.did)
    ref = await personal.save(_EntityDraft(content), access)
    return await service.promote(personal, ref.identifier, access, identity)


async def test_two_agents_promoting_the_same_entity_merge_into_one_canonical(
    tmp_path: Path,
) -> None:
    """Agent A and agent B promote "Acme"; the shared store holds ONE canonical doc."""
    agent_a = AgentIdentity.generate("test", "agent-a")
    agent_b = AgentIdentity.generate("test", "agent-b")
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)

    await _promote_entity(service, tmp_path, agent_a, "Acme\nrole: vendor\nlead_time: 2 weeks")
    await _promote_entity(service, tmp_path, agent_b, "Acme\nrole: partner\nlead_time: 2 weeks")

    # RED: two separate documents exist today; the merge must collapse them to one.
    assert len(_entity_docs(service)) == 1, "promoting the same entity twice left a duplicate"


async def test_conflicting_field_keeps_both_values_with_per_agent_provenance(
    tmp_path: Path,
) -> None:
    """A conflicting fact (vendor vs partner) is retained for BOTH agents, attributed."""
    agent_a = AgentIdentity.generate("test", "agent-a")
    agent_b = AgentIdentity.generate("test", "agent-b")
    reader = AgentIdentity.generate("test", "reader")
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)

    await _promote_entity(service, tmp_path, agent_a, "Acme\nrole: vendor")
    await _promote_entity(service, tmp_path, agent_b, "Acme\nrole: partner")

    summaries = await service.list_documents(_Access(reader))
    assert len(summaries) == 1, "expected a single merged canonical entity"
    merged = await service.read(summaries[0].reference.identifier, _Access(reader))

    # Both conflicting values survive the merge...
    assert "vendor" in merged.content
    assert "partner" in merged.content
    # ...and each is attributable to the agent that contributed it (provenance).
    assert agent_a.did in merged.content
    assert agent_b.did in merged.content


async def test_repromoting_the_same_entity_is_idempotent(tmp_path: Path) -> None:
    """A, then B, then A again -> still exactly one canonical entity document."""
    agent_a = AgentIdentity.generate("test", "agent-a")
    agent_b = AgentIdentity.generate("test", "agent-b")
    service = FleetSharedKnowledgeService.for_arc_team(tmp_path)

    await _promote_entity(service, tmp_path, agent_a, "Acme\nrole: vendor")
    await _promote_entity(service, tmp_path, agent_b, "Acme\nrole: partner")
    await _promote_entity(service, tmp_path, agent_a, "Acme\nrole: vendor")

    assert len(_entity_docs(service)) == 1, "re-promoting duplicated the canonical entity"
