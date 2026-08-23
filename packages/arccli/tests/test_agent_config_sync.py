"""`arc agent config --sync` backfills settings without touching operator values."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import tomlkit

from arccli.commands.agent._common import render_agent_config
from arccli.commands.agent._config_sync import (
    discover_agent_dirs,
    sync_agent_config,
)


def _write_agent(agent_dir: Path, body: str) -> Path:
    agent_dir.mkdir(parents=True, exist_ok=True)
    config = agent_dir / "arcagent.toml"
    config.write_text(body, encoding="utf-8")
    return config


@pytest.fixture
def stale_agent(tmp_path: Path) -> Path:
    """An agent scaffolded before several modules gained settings."""
    agent_dir = tmp_path / "team" / "olivia"
    _write_agent(
        agent_dir,
        """
[agent]
name = "Olivia"
tier = "personal"

[identity]
did = "did:arc:local:olivia/abcd"

[modules.memory]
enabled = true
priority = 100

[modules.memory.config]
brain = "arcmemory"
top_k = 9

[modules.connectors]
enabled = true
priority = 100
""".lstrip(),
    )
    return agent_dir


def test_sync_adds_missing_module_and_settings(stale_agent: Path) -> None:
    result = sync_agent_config(stale_agent)

    assert result.written
    merged = tomllib.loads((stale_agent / "arcagent.toml").read_text(encoding="utf-8"))
    connected = merged["modules"]["connected_data"]
    assert connected["enabled"] is True
    assert connected["config"]["interval_seconds"] == 3600.0
    assert "working_set_enabled" in merged["modules"]["memory"]["config"]
    assert "modules.connected_data" in result.added


def test_sync_never_overwrites_an_operator_value(stale_agent: Path) -> None:
    sync_agent_config(stale_agent)

    merged = tomllib.loads((stale_agent / "arcagent.toml").read_text(encoding="utf-8"))
    assert merged["modules"]["memory"]["config"]["top_k"] == 9
    assert merged["modules"]["memory"]["config"]["brain"] == "arcmemory"


def test_sync_leaves_identity_alone(stale_agent: Path) -> None:
    sync_agent_config(stale_agent)

    merged = tomllib.loads((stale_agent / "arcagent.toml").read_text(encoding="utf-8"))
    assert merged["agent"]["name"] == "Olivia"
    assert merged["identity"]["did"] == "did:arc:local:olivia/abcd"


def test_sync_is_idempotent(stale_agent: Path) -> None:
    sync_agent_config(stale_agent)
    after_first = (stale_agent / "arcagent.toml").read_text(encoding="utf-8")

    second = sync_agent_config(stale_agent)

    assert second.added == ()
    assert not second.written
    assert (stale_agent / "arcagent.toml").read_text(encoding="utf-8") == after_first


def test_dry_run_reports_without_writing(stale_agent: Path) -> None:
    before = (stale_agent / "arcagent.toml").read_text(encoding="utf-8")

    result = sync_agent_config(stale_agent, dry_run=True)

    assert result.added
    assert not result.written
    assert (stale_agent / "arcagent.toml").read_text(encoding="utf-8") == before


def test_a_freshly_scaffolded_agent_needs_no_sync(tmp_path: Path) -> None:
    agent_dir = tmp_path / "team" / "fresh"
    _write_agent(
        agent_dir,
        render_agent_config(name="Fresh", did="did:arc:local:fresh/0001"),
    )

    result = sync_agent_config(agent_dir)

    assert result.added == ()


def test_sync_preserves_comments(stale_agent: Path) -> None:
    config = stale_agent / "arcagent.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "top_k = 9", "top_k = 9  # tuned for this agent"
        ),
        encoding="utf-8",
    )

    sync_agent_config(stale_agent)

    doc = tomlkit.parse(config.read_text(encoding="utf-8"))
    assert "tuned for this agent" in tomlkit.dumps(doc)


def test_discover_agent_dirs_finds_every_fleet_member(tmp_path: Path) -> None:
    team_root = tmp_path / "team"
    for name in ("beta", "alpha"):
        _write_agent(team_root / name, "[agent]\nname = 'x'\n")
    (team_root / "notanagent").mkdir()

    found = discover_agent_dirs(team_root)

    assert [path.name for path in found] == ["alpha", "beta"]


def test_synced_config_still_loads(stale_agent: Path) -> None:
    """The merged file must satisfy the real config loader, not just tomllib."""
    import arcagent

    sync_agent_config(stale_agent)

    config = arcagent.load_config(stale_agent / "arcagent.toml")
    assert config.modules["connected_data"].enabled
