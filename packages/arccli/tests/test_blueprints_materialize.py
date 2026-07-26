"""materialize_blueprint — write the full v2 surface into a concrete agent dir.

Exercises the real path: resolve a v2 blueprint folder, materialize it into a tmp
agent home, and assert every artifact class lands — sibling tomls merged, persona
written, prompt overlays authored + signed, capabilities/skills copied + signed,
schedules seeded and round-tripping through the scheduler store.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest

from arccli import blueprints as bp
from arccli.blueprints_materialize import materialize_blueprint


def _write_v2_blueprint(root: Path) -> Path:
    d = root / "sales-mat"
    (d / "prompts" / "arcmemory").mkdir(parents=True)
    (d / "capabilities").mkdir()
    (d / "skills" / "deal-review").mkdir(parents=True)
    (d / "blueprint.toml").write_text(
        "[blueprint]\n"
        'name = "sales-mat"\nversion = "1.0.0"\ntier = "personal"\n\n'
        "[modules.memory]\nenabled = true\n\n"
        "[arcllm.llm]\nmodel = \"anthropic/claude-sonnet-5\"\n\n"
        "[arcrun]\nmax_turns = 40\n\n"
        "[[schedules]]\n"
        'type = "cron"\nexpression = "0 8 * * *"\n'
        'prompt = "Give me the morning pipeline briefing."\n',
        encoding="utf-8",
    )
    (d / "persona.md").write_text("You are a sales chief of staff.\n", encoding="utf-8")
    (d / "prompts" / "arcmemory" / "distill_fact.md").write_text(
        "Extract contacts, companies, and deals.\n", encoding="utf-8"
    )
    (d / "capabilities" / "crm.py").write_text("# crm verbs\ndef noop():\n    return 1\n", "utf-8")
    (d / "skills" / "deal-review" / "SKILL.md").write_text(
        "---\nname: deal-review\ndescription: review a deal thoroughly for risk and next steps\n"
        "---\n\n## Files\n\n## Contract\n\n## Knowledge\n\n## Steps\ndo x\n## Output\n\n"
        "## Red Flags & Rationalizations\n\n## Validation\n\n## Examples\n",
        encoding="utf-8",
    )
    return d


def _agent_dir(root: Path) -> Path:
    agent = root / "agent"
    (agent / "workspace").mkdir(parents=True)
    (agent / "arcagent.toml").write_text('[agent]\nname = "sales"\n', encoding="utf-8")
    return agent


def test_materialize_writes_full_surface(tmp_path: Path) -> None:
    blueprint = bp.resolve_blueprint(str(_write_v2_blueprint(tmp_path)), tier="personal")
    agent = _agent_dir(tmp_path)
    result = materialize_blueprint(
        blueprint,
        agent,
        deployment_tier="personal",
        operator_signer=("operator:test", os.urandom(32)),
        agent_signer=("did:agent:test", os.urandom(32)),
    )

    # sibling tomls merged
    assert tomllib.loads((agent / "arcllm.toml").read_text())["llm"]["model"] == (
        "anthropic/claude-sonnet-5"
    )
    assert tomllib.loads((agent / "arcrun.toml").read_text())["max_turns"] == 40
    # persona
    assert result.wrote_identity
    assert "chief of staff" in (agent / "workspace" / "identity.md").read_text()
    # prompt overlay authored + signed
    overlay = agent / "context" / "arcmemory" / "distill_fact.md"
    assert overlay.is_file() and Path(f"{overlay}.arcsig").is_file()
    assert "arcmemory/distill_fact" in result.prompt_overlays
    # capability copied + signed
    cap = agent / "capabilities" / "crm.py"
    assert cap.is_file() and Path(f"{cap}.arcsig").is_file()
    # skill copied + signed
    skill_md = agent / "capabilities" / "skills" / "deal-review" / "SKILL.md"
    assert skill_md.is_file() and Path(f"{skill_md}.arcsig").is_file()


def test_materialize_seeds_schedules_roundtrip(tmp_path: Path) -> None:
    from arcagent.modules.scheduler.store import ScheduleStore

    blueprint = bp.resolve_blueprint(str(_write_v2_blueprint(tmp_path)), tier="personal")
    agent = _agent_dir(tmp_path)
    result = materialize_blueprint(
        blueprint, agent, deployment_tier="personal", operator_signer=("op", os.urandom(32))
    )
    assert result.schedules == 1
    entries = ScheduleStore(agent / "workspace" / "schedules.json").load()
    assert entries[0].type == "cron"
    assert entries[0].expression == "0 8 * * *"
    assert entries[0].metadata.created_by == "system"


def test_materialize_refuses_unsigned_prompt_overlays(tmp_path: Path) -> None:
    blueprint = bp.resolve_blueprint(str(_write_v2_blueprint(tmp_path)), tier="personal")
    agent = _agent_dir(tmp_path)
    with pytest.raises(ValueError, match="operator key"):
        materialize_blueprint(blueprint, agent, deployment_tier="personal", operator_signer=None)


def test_materialize_never_clobbers_existing_identity(tmp_path: Path) -> None:
    blueprint = bp.resolve_blueprint(str(_write_v2_blueprint(tmp_path)), tier="personal")
    agent = _agent_dir(tmp_path)
    (agent / "workspace" / "identity.md").write_text("MY OWN PERSONA\n", encoding="utf-8")
    result = materialize_blueprint(
        blueprint, agent, deployment_tier="personal", operator_signer=("op", os.urandom(32))
    )
    assert not result.wrote_identity
    assert (agent / "workspace" / "identity.md").read_text() == "MY OWN PERSONA\n"
