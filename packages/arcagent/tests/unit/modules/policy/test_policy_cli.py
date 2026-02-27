"""Tests for policy CLI commands — read-only workspace inspection."""

from __future__ import annotations

from pathlib import Path

import click.testing
import pytest

from arcagent.modules.policy.cli import cli_group


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """Create a workspace with fixture data for policy CLI tests."""
    ws = tmp_path / "workspace"
    ws.mkdir()

    # Policy file with structured bullets
    bullets = [
        "- [P01] Be helpful {score:8, uses:5, reviewed:2026-02-15, created:2026-01-01, source:s1}",
        "- [P02] Use tools {score:6, uses:3, reviewed:2026-02-14, created:2026-01-05, source:s2}",
        "- [P03] Report errors {score:9, uses:10,"
        " reviewed:2026-02-15, created:2026-01-01, source:s3}",
    ]
    policy_content = "# Policy\n\n" + "\n".join(bullets) + "\n"
    (ws / "policy.md").write_text(policy_content)

    # arcagent.toml (lives one level up from workspace)
    toml_content = """\
[agent]
name = "test-agent"

[modules.policy]
enabled = true

[modules.policy.config]
eval_interval_turns = 10
max_bullets = 100
"""
    (tmp_path / "arcagent.toml").write_text(toml_content)

    # Sessions directory with a dummy session (for history command)
    sessions_dir = ws / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "abc123.jsonl").write_text('{"role":"user","content":"hello"}\n')

    return ws


def _invoke(workspace: Path, args: list[str]) -> click.testing.Result:
    """Invoke a policy CLI command via CliRunner."""
    group = cli_group(workspace)
    runner = click.testing.CliRunner()
    return runner.invoke(group, args)


class TestBulletsCommand:
    """Tests for ``arc agent policy <path> bullets``."""

    def test_lists_bullets(self, workspace: Path) -> None:
        result = _invoke(workspace, ["bullets"])
        assert result.exit_code == 0
        assert "P01" in result.output
        assert "P02" in result.output
        assert "P03" in result.output

    def test_sort_by_score(self, workspace: Path) -> None:
        result = _invoke(workspace, ["bullets", "--sort", "score"])
        assert result.exit_code == 0
        # P03 (score 9) should appear before P01 (score 8)
        p03_pos = result.output.index("P03")
        p01_pos = result.output.index("P01")
        assert p03_pos < p01_pos

    def test_sort_by_created(self, workspace: Path) -> None:
        result = _invoke(workspace, ["bullets", "--sort", "created"])
        assert result.exit_code == 0
        # P02 (created 2026-01-05) should appear before P01 (created 2026-01-01)
        p02_pos = result.output.index("P02")
        p01_pos = result.output.index("P01")
        assert p02_pos < p01_pos

    def test_no_policy_file(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()
        result = _invoke(ws, ["bullets"])
        assert result.exit_code == 0
        assert "No policy.md" in result.output

    def test_no_structured_bullets(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "policy.md").write_text("# Policy\n\n- Be helpful\n- Be direct\n")
        result = _invoke(ws, ["bullets"])
        assert result.exit_code == 0
        assert "No structured bullets" in result.output


class TestConfigCommand:
    """Tests for ``arc agent policy <path> config``."""

    def test_shows_config(self, workspace: Path) -> None:
        result = _invoke(workspace, ["config"])
        assert result.exit_code == 0
        assert "eval_interval_turns" in result.output
        assert "max_bullets" in result.output

    def test_shows_defaults_without_toml(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()
        result = _invoke(ws, ["config"])
        assert result.exit_code == 0
        # Should show defaults
        assert "eval_interval_turns" in result.output
        assert "20" in result.output  # default value


class TestHistoryCommand:
    """Tests for ``arc agent policy <path> history``."""

    def test_history_with_sessions(self, workspace: Path) -> None:
        result = _invoke(workspace, ["history"])
        assert result.exit_code == 0
        # Should mention telemetry-based logging
        assert "telemetry" in result.output.lower() or "Sessions available" in result.output

    def test_history_no_sessions(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()
        result = _invoke(ws, ["history"])
        assert result.exit_code == 0
        assert "No sessions directory" in result.output
