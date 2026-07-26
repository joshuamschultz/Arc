"""E2E — the shipped sales-exec-assistant blueprint materializes into a real agent home.

Proves the packaged blueprint (not a fixture) resolves and lands every artifact class:
sibling tomls, persona, seven signed revenue-lens prompt overlays, the signed CRM
capability, three signed sales skills, and a seeded morning-briefing schedule that the
scheduler store reads back.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from arccli import blueprints as bp
from arccli.blueprints_materialize import materialize_blueprint


def _agent(root: Path) -> Path:
    agent = root / "agent"
    (agent / "workspace").mkdir(parents=True)
    (agent / "arcagent.toml").write_text('[agent]\nname = "sales"\n', encoding="utf-8")
    return agent


def test_sales_blueprint_materializes_full_surface(tmp_path: Path) -> None:
    blueprint = bp.resolve_blueprint("sales-exec-assistant", tier="personal")
    agent = _agent(tmp_path)
    result = materialize_blueprint(
        blueprint,
        agent,
        deployment_tier="personal",
        operator_signer=("operator:test", os.urandom(32)),
        agent_signer=("did:agent:test", os.urandom(32)),
    )

    # sibling config
    assert tomllib.loads((agent / "arcllm.toml").read_text())["llm"]["model"] == (
        "anthropic/claude-sonnet-5"
    )
    assert tomllib.loads((agent / "arcrun.toml").read_text())["max_turns"] == 40
    # modules merged into arcagent.toml
    cfg = tomllib.loads((agent / "arcagent.toml").read_text())
    assert cfg["modules"]["memory"]["enabled"] is True
    assert cfg["modules"]["tasks"]["enabled"] is True
    assert cfg["modules"]["scheduler"]["enabled"] is True

    # persona
    assert result.wrote_identity
    assert "chief of staff" in (agent / "workspace" / "identity.md").read_text().lower()

    # seven signed revenue-lens prompt overlays
    assert len(result.prompt_overlays) == 7
    fact = agent / "context" / "arcmemory" / "distill_fact.md"
    assert fact.is_file() and Path(f"{fact}.arcsig").is_file()
    assert "deal" in fact.read_text().lower()

    # signed CRM capability at the per-agent capabilities root
    crm = agent / "capabilities" / "crm.py"
    assert crm.is_file() and Path(f"{crm}.arcsig").is_file()
    assert "crm_log_deal" in crm.read_text()

    # three signed sales skills at the per-agent skills root
    skills_root = agent / "capabilities" / "skills"
    assert {p.name for p in skills_root.iterdir()} == {
        "pre-call-brief",
        "deal-review",
        "follow-up-sweep",
    }
    for skill in skills_root.iterdir():
        assert (skill / "SKILL.md").is_file()
        assert (skill / f"SKILL.md{'.arcsig'}").is_file()

    # seeded morning briefing schedule
    from arcagent.modules.scheduler.store import ScheduleStore

    assert result.schedules == 1
    entries = ScheduleStore(agent / "workspace" / "schedules.json").load()
    assert entries[0].type == "cron"
    assert entries[0].expression == "0 8 * * 1-5"
