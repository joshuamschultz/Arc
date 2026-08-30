"""H-027 — the always-on fleet wires the signed shared-knowledge surface LIVE."""

from __future__ import annotations

import arcagent
import pytest
from arcmemory.adapters.personal_knowledge import PersonalKnowledgeAdapter
from arctrust import AuditSink

from arccli.commands._serve import install_fleet_shared_knowledge

_SHARED_TOOLS = {
    "shared_knowledge_promote",
    "shared_knowledge_retrieve",
    "shared_knowledge_search",
    "shared_knowledge_revoke",
}


class _Draft:
    title = "Release checklist"
    content = "Run the verified release steps in order."
    classification = "UNCLASSIFIED"
    tags = ("release",)
    document_type = "procedure"


def _agent_config(tmp_path, name: str) -> arcagent.ArcAgentConfig:
    return arcagent.ArcAgentConfig.model_validate(
        {
            "agent": {
                "name": name,
                "org": "test",
                "type": "executor",
                "workspace": str(tmp_path / name / "workspace"),
            },
            "llm": {"model": "test/model"},
            "identity": {"key_dir": str(tmp_path / name / "keys")},
            "telemetry": {"enabled": False},
            "modules": {"memory": {"curated_knowledge_enabled": True}},
        }
    )


@pytest.mark.asyncio
async def test_install_makes_promote_live_and_audits_through_the_agent_sink(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    team_root = tmp_path / "team"
    team_root.mkdir()
    publisher = arcagent.ArcAgent(_agent_config(tmp_path, "publisher"))
    reader = arcagent.ArcAgent(_agent_config(tmp_path, "reader"))
    await publisher.startup()
    await reader.startup()
    try:
        # Nothing is wired before the fleet composition runs.
        assert not _SHARED_TOOLS & {t.name for t in publisher.registered_tools}

        started = [
            (publisher, publisher.workspace, "UNCLASSIFIED"),
            (reader, reader.workspace, "UNCLASSIFIED"),
        ]
        count = await install_fleet_shared_knowledge(team_root, started)
        assert count == 2

        # LIVE: every started member now carries the fleet knowledge tools.
        assert _SHARED_TOOLS <= {t.name for t in publisher.registered_tools}
        assert _SHARED_TOOLS <= {t.name for t in reader.registered_tools}

        # A real, non-None audit sink is wired (not a silent side channel).
        assert isinstance(publisher.audit_sink, AuditSink)

        # Drive a promotion through the SAME service, signer, and sink the
        # composition wired, then prove the doc is fleet-readable and audited.
        publisher_access = arcagent.KnowledgeAccess(publisher.did, "UNCLASSIFIED")
        reader_access = arcagent.KnowledgeAccess(reader.did, "UNCLASSIFIED")
        personal = PersonalKnowledgeAdapter(publisher.workspace, publisher.did)
        personal_ref = await personal.save(_Draft(), publisher_access)

        from arcteam.shared_knowledge import FleetSharedKnowledgeService

        # The agent's real telemetry sink records what it is handed — proving the
        # composition audits a promotion through the agent, not a silent channel.
        recorded: list[str] = []
        real_sink = publisher.audit_sink
        original_write = real_sink.write

        def _capturing_write(event: object) -> None:
            recorded.append(getattr(event, "action", ""))
            original_write(event)

        object.__setattr__(real_sink, "write", _capturing_write)

        service = FleetSharedKnowledgeService.for_team_root(team_root)
        shared_ref = await service.promote(
            personal,
            personal_ref.identifier,
            publisher_access,
            publisher.extension_signer,
            audit_sink=real_sink,
        )

        # The shared collection lives under the fleet's team root and the peer reads it.
        assert service.backend.root == (team_root / "shared" / "knowledge").resolve()
        assert (service.backend.root / "documents" / f"{shared_ref.identifier}.md").exists()
        retrieved = await service.read(shared_ref.identifier, reader_access)
        assert retrieved.content == _Draft.content
        # The promote audited through the promoting agent's telemetry sink.
        assert "knowledge.collection_saved" in recorded
    finally:
        await publisher.shutdown()
        await reader.shutdown()
