"""Tests for arcgateway.team_roster — discover agents on disk + overlay live status."""

from __future__ import annotations

from pathlib import Path

import pytest

from arcgateway.team_roster import RosterEntry, list_team


@pytest.fixture
def team_root(tmp_path: Path) -> Path:
    """Synthetic team/ dir with three agents, mixed online/offline, varying [ui]."""
    root = tmp_path / "team"
    root.mkdir()

    # Alice — full [ui], named 'alice', model 'anthropic/sonnet'
    alice = root / "alice_agent"
    alice.mkdir()
    (alice / "arcagent.toml").write_text(
        '[agent]\n'
        'name = "alice"\n'
        'org = "research"\n'
        'type = "curator"\n'
        '[identity]\n'
        'did = "did:arc:agent:alice"\n'
        '[llm]\n'
        'model = "anthropic/claude-sonnet-4-6"\n'
        '[ui]\n'
        'display_name = "Alice the Curator"\n'
        'color = "#ff6b6b"\n'
        'role_label = "policy curator"\n'
        'hidden = false\n',
        encoding="utf-8",
    )

    # Bob — no [ui], plain config
    bob = root / "bob_agent"
    bob.mkdir()
    (bob / "arcagent.toml").write_text(
        '[agent]\n'
        'name = "bob"\n'
        'org = "ops"\n'
        'type = "responder"\n'
        '[identity]\n'
        'did = "did:arc:agent:bob"\n'
        '[llm]\n'
        'model = "openai/gpt-4o"\n',
        encoding="utf-8",
    )

    # Carol — hidden=true, minimal config
    carol = root / "carol_agent"
    carol.mkdir()
    (carol / "arcagent.toml").write_text(
        '[agent]\n'
        'name = "carol"\n'
        '[ui]\n'
        'hidden = true\n',
        encoding="utf-8",
    )

    # Skipped: dir without arcagent.toml
    (root / "stub_agent").mkdir()

    # Skipped: dir not matching *_agent
    (root / "not-an-agent").mkdir()

    return root


class TestDiscovery:
    def test_finds_all_agent_dirs(self, team_root: Path) -> None:
        roster = list_team(team_root=team_root, online_ids=set())
        ids = {r.agent_id for r in roster}
        assert ids == {"alice", "bob", "carol"}

    def test_skips_dirs_without_arcagent_toml(self, team_root: Path) -> None:
        roster = list_team(team_root=team_root, online_ids=set())
        ids = {r.agent_id for r in roster}
        assert "stub" not in ids

    def test_skips_dirs_not_matching_pattern(self, team_root: Path) -> None:
        roster = list_team(team_root=team_root, online_ids=set())
        ids = {r.agent_id for r in roster}
        assert "not-an-agent" not in ids

    def test_returns_RosterEntry(self, team_root: Path) -> None:
        roster = list_team(team_root=team_root, online_ids=set())
        assert all(isinstance(r, RosterEntry) for r in roster)

    def test_deterministic_order(self, team_root: Path) -> None:
        roster1 = list_team(team_root=team_root, online_ids=set())
        roster2 = list_team(team_root=team_root, online_ids=set())
        assert [r.agent_id for r in roster1] == [r.agent_id for r in roster2]


class TestOnlineOverlay:
    def test_marks_online(self, team_root: Path) -> None:
        roster = list_team(team_root=team_root, online_ids={"alice", "bob"})
        by_id = {r.agent_id: r for r in roster}
        assert by_id["alice"].online is True
        assert by_id["bob"].online is True
        assert by_id["carol"].online is False

    def test_all_offline_by_default(self, team_root: Path) -> None:
        roster = list_team(team_root=team_root, online_ids=set())
        assert all(r.online is False for r in roster)


class TestUISection:
    def test_ui_fields_propagated(self, team_root: Path) -> None:
        roster = list_team(team_root=team_root, online_ids=set())
        alice = next(r for r in roster if r.agent_id == "alice")
        assert alice.display_name == "Alice the Curator"
        assert alice.color == "#ff6b6b"
        assert alice.role_label == "policy curator"
        assert alice.hidden is False

    def test_defaults_when_ui_absent(self, team_root: Path) -> None:
        roster = list_team(team_root=team_root, online_ids=set())
        bob = next(r for r in roster if r.agent_id == "bob")
        # display_name falls back to agent.name
        assert bob.display_name == "bob"
        # role_label falls back to agent.type
        assert bob.role_label == "responder"
        # color is deterministic hash, must be 7 chars (#rrggbb).
        assert bob.color is not None
        assert bob.color.startswith("#")
        assert len(bob.color) == 7
        assert bob.hidden is False

    def test_hidden_propagated(self, team_root: Path) -> None:
        roster = list_team(team_root=team_root, online_ids=set())
        carol = next(r for r in roster if r.agent_id == "carol")
        assert carol.hidden is True


class TestDeterministicColor:
    def test_same_id_same_color(self, team_root: Path) -> None:
        r1 = list_team(team_root=team_root, online_ids=set())
        r2 = list_team(team_root=team_root, online_ids=set())
        c1 = next(r.color for r in r1 if r.agent_id == "bob")
        c2 = next(r.color for r in r2 if r.agent_id == "bob")
        assert c1 == c2

    def test_different_ids_different_colors(self, team_root: Path) -> None:
        roster = list_team(team_root=team_root, online_ids=set())
        bob_color = next(r.color for r in roster if r.agent_id == "bob")
        carol_color = next(r.color for r in roster if r.agent_id == "carol")
        # carol overrides nothing in [ui] for color, so falls to hash.
        # Statistically these must differ for distinct ids.
        assert bob_color != carol_color


class TestEdgeCases:
    def test_missing_team_root_returns_empty(self, tmp_path: Path) -> None:
        ghost = tmp_path / "does_not_exist"
        roster = list_team(team_root=ghost, online_ids=set())
        assert roster == []

    def test_malformed_toml_skipped(self, tmp_path: Path) -> None:
        root = tmp_path / "team"
        bad = root / "broken_agent"
        bad.mkdir(parents=True)
        # Invalid TOML — unterminated string.
        (bad / "arcagent.toml").write_text('[agent]\nname = "', encoding="utf-8")

        # Also include a good agent so we can confirm it survives.
        good = root / "good_agent"
        good.mkdir()
        (good / "arcagent.toml").write_text(
            '[agent]\nname = "good"\n', encoding="utf-8"
        )

        roster = list_team(team_root=root, online_ids=set())
        ids = {r.agent_id for r in roster}
        assert "good" in ids
        assert "broken" not in ids

    def test_non_directory_pattern_match_skipped(self, tmp_path: Path) -> None:
        root = tmp_path / "team"
        root.mkdir()
        # File named '*_agent' (not a dir).
        (root / "fake_agent").write_text("not-a-dir", encoding="utf-8")
        roster = list_team(team_root=root, online_ids=set())
        assert roster == []


class TestProviderInference:
    def test_provider_inferred_from_slash(self, team_root: Path) -> None:
        roster = list_team(team_root=team_root, online_ids=set())
        alice = next(r for r in roster if r.agent_id == "alice")
        bob = next(r for r in roster if r.agent_id == "bob")
        assert alice.provider == "anthropic"
        assert bob.provider == "openai"

    def test_no_model_no_provider(self, team_root: Path) -> None:
        roster = list_team(team_root=team_root, online_ids=set())
        carol = next(r for r in roster if r.agent_id == "carol")
        assert carol.provider is None
        assert carol.model is None
