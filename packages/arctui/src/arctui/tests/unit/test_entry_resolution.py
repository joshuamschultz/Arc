"""SPEC-058 T-765: entry-point agent resolution wiring (REQ-141, REQ-143).

entry.py must resolve which agent to drive from the roster (+ optional --agent),
falling back to a cwd arcagent.toml for the in-an-agent-dir case, and surface a
clear message (offer create / pass --agent) instead of silent no-agent mode.
"""

from __future__ import annotations

from pathlib import Path

from arctui.entry import _resolve_agent_config


def _make_agent(team_root: Path, name: str) -> None:
    root = team_root / name
    (root / "workspace").mkdir(parents=True)
    (root / "arcagent.toml").write_text(
        f'[agent]\nname = "{name}"\n[llm]\nmodel = "anthropic/claude-sonnet-5"\n', encoding="utf-8"
    )


def test_resolve_single_roster_agent(tmp_path: Path) -> None:
    team = tmp_path / "team"
    _make_agent(team, "coder")
    path, msg = _resolve_agent_config([], cwd=tmp_path / "proj", team_root=team)
    assert path == team / "coder" / "arcagent.toml"
    assert msg is None


def test_resolve_by_agent_flag(tmp_path: Path) -> None:
    team = tmp_path / "team"
    _make_agent(team, "coder")
    _make_agent(team, "researcher")
    path, msg = _resolve_agent_config(
        ["--agent", "researcher"], cwd=tmp_path / "proj", team_root=team
    )
    assert path == team / "researcher" / "arcagent.toml"
    assert msg is None


def test_resolve_ambiguous_asks_for_flag(tmp_path: Path) -> None:
    team = tmp_path / "team"
    _make_agent(team, "coder")
    _make_agent(team, "researcher")
    path, msg = _resolve_agent_config([], cwd=tmp_path / "proj", team_root=team)
    assert path is None
    assert msg is not None and "--agent" in msg and "coder" in msg and "researcher" in msg


def test_resolve_empty_offers_create(tmp_path: Path) -> None:
    path, msg = _resolve_agent_config([], cwd=tmp_path / "proj", team_root=tmp_path / "empty")
    assert path is None
    assert msg is not None and "arc agent create" in msg


def test_resolve_falls_back_to_cwd_agent_dir(tmp_path: Path) -> None:
    # Launched from inside an agent dir with no roster → use the local arcagent.toml.
    proj = tmp_path / "an_agent"
    (proj / "workspace").mkdir(parents=True)
    (proj / "arcagent.toml").write_text('[agent]\nname = "x"\n', encoding="utf-8")
    path, msg = _resolve_agent_config([], cwd=proj, team_root=tmp_path / "empty")
    assert path == proj / "arcagent.toml"
    assert msg is None


def test_resolve_unknown_name_lists_available(tmp_path: Path) -> None:
    team = tmp_path / "team"
    _make_agent(team, "coder")
    path, msg = _resolve_agent_config(["--agent", "ghost"], cwd=tmp_path / "proj", team_root=team)
    assert path is None
    assert msg is not None and "coder" in msg
