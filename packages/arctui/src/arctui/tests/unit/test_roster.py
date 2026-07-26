"""SPEC-058 T-764: agent roster enumeration + selection (REQ-143).

arctui must select which agent to drive from the arc roster, not the CWD-only
load in entry.py. These cover the pure enumeration/selection core; the
interactive picker screen is a thin layer over ``select_agent``.
"""

from __future__ import annotations

from pathlib import Path

from arctui.roster import AgentRef, list_agents, select_agent


def _make_agent(team_root: Path, name: str, model: str = "anthropic/claude-sonnet-5") -> None:
    """Write a minimal parseable agent dir under team_root."""
    root = team_root / name
    (root / "workspace").mkdir(parents=True)
    (root / "arcagent.toml").write_text(
        f'[agent]\nname = "{name}"\n[llm]\nmodel = "{model}"\n', encoding="utf-8"
    )


def test_list_agents_empty_root_returns_empty(tmp_path: Path) -> None:
    assert list_agents(tmp_path / "does_not_exist") == []


def test_list_agents_enumerates_roster(tmp_path: Path) -> None:
    team = tmp_path / "team"
    _make_agent(team, "coder")
    _make_agent(team, "researcher")
    agents = list_agents(team)
    assert {a.agent_id for a in agents} == {"coder", "researcher"}
    coder = next(a for a in agents if a.agent_id == "coder")
    assert coder.config_path == team / "coder" / "arcagent.toml"
    assert coder.config_path.is_file()


def _refs() -> list[AgentRef]:
    return [
        AgentRef(agent_id="coder", display_name="Coder", root=Path("/a"),
                 config_path=Path("/a/arcagent.toml"), model="m"),
        AgentRef(agent_id="researcher", display_name="Researcher", root=Path("/b"),
                 config_path=Path("/b/arcagent.toml"), model="m"),
    ]


def test_select_agent_by_name() -> None:
    by_id = select_agent(_refs(), "researcher")
    assert by_id is not None and by_id.agent_id == "researcher"
    by_display = select_agent(_refs(), "Coder")  # display name too
    assert by_display is not None and by_display.agent_id == "coder"


def test_select_agent_unknown_name_is_none() -> None:
    assert select_agent(_refs(), "ghost") is None


def test_select_agent_single_auto_selects() -> None:
    one = _refs()[:1]
    chosen = select_agent(one, None)
    assert chosen is not None and chosen.agent_id == "coder"


def test_select_agent_multiple_without_name_needs_picker() -> None:
    # None signals the caller must present the interactive picker.
    assert select_agent(_refs(), None) is None


def test_resolve_agent_selected_single(tmp_path: Path) -> None:
    from arctui.roster import resolve_agent

    team = tmp_path / "team"
    _make_agent(team, "coder")
    res = resolve_agent(name=None, team_root=team)
    assert res.selected is not None and res.selected.agent_id == "coder"
    assert res.reason == "single"


def test_resolve_agent_by_name(tmp_path: Path) -> None:
    from arctui.roster import resolve_agent

    team = tmp_path / "team"
    _make_agent(team, "coder")
    _make_agent(team, "researcher")
    res = resolve_agent(name="researcher", team_root=team)
    assert res.selected is not None and res.selected.agent_id == "researcher"
    assert res.reason == "named"


def test_resolve_agent_ambiguous_needs_pick(tmp_path: Path) -> None:
    from arctui.roster import resolve_agent

    team = tmp_path / "team"
    _make_agent(team, "coder")
    _make_agent(team, "researcher")
    res = resolve_agent(name=None, team_root=team)
    assert res.selected is None
    assert res.reason == "ambiguous"
    assert {a.agent_id for a in res.candidates} == {"coder", "researcher"}


def test_resolve_agent_none_offers_create(tmp_path: Path) -> None:
    from arctui.roster import resolve_agent

    res = resolve_agent(name=None, team_root=tmp_path / "empty")
    assert res.selected is None and res.candidates == []
    assert res.reason == "empty"


def test_resolve_agent_unknown_name(tmp_path: Path) -> None:
    from arctui.roster import resolve_agent

    team = tmp_path / "team"
    _make_agent(team, "coder")
    res = resolve_agent(name="ghost", team_root=team)
    assert res.selected is None and res.reason == "unknown"
