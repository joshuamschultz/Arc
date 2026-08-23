"""Tests for ArcTeam's public ArcAgent shared-knowledge attachment."""

from __future__ import annotations

import pytest
from arcmemory.adapters.personal_knowledge import PersonalKnowledgeAdapter
from arctrust import AgentIdentity

from arcteam.shared_knowledge import FleetSharedKnowledgeService, SharedKnowledgeAttachment


class _Access:
    def __init__(self, identity: AgentIdentity) -> None:
        self.caller_did = identity.did
        self.clearance = "UNCLASSIFIED"


class _Draft:
    title = "Runbook"
    content = "Rotate the key."
    classification = "UNCLASSIFIED"
    tags = ()
    document_type = "note"


@pytest.mark.asyncio
async def test_attachment_exposes_shared_lifecycle_tools_and_composes_promotion(tmp_path) -> None:
    identity = AgentIdentity.generate("test", "attachment")
    access = _Access(identity)
    personal = PersonalKnowledgeAdapter(tmp_path / "agent", identity.did)
    personal_ref = await personal.save(_Draft(), access)
    attachment = SharedKnowledgeAttachment(
        FleetSharedKnowledgeService.for_arc_team(tmp_path),
        personal_knowledge=personal,
        access=access,
        signer=identity,
    )

    assert {tool.name for tool in await attachment.describe_tools()} == {
        "shared_knowledge_promote",
        "shared_knowledge_retrieve",
        "shared_knowledge_search",
        "shared_knowledge_revoke",
    }
    promoted = await attachment.invoke(
        "shared_knowledge_promote", {"reference": personal_ref.identifier}
    )
    identifier = promoted.content.removesuffix(".").split()[-1]
    retrieved = await attachment.invoke("shared_knowledge_retrieve", {"reference": identifier})

    assert promoted.outcome.value == "ok"
    assert "Rotate the key." in retrieved.content
