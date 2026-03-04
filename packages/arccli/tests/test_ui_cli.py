"""Tests for arc ui subcommands — SPEC-016 Phase 7."""

from click.testing import CliRunner

from arccli.main import cli

runner = CliRunner()


class TestUIGroup:
    def test_ui_help(self):
        result = runner.invoke(cli, ["ui", "--help"])
        assert result.exit_code == 0
        assert "start" in result.output

    def test_ui_start_help(self):
        result = runner.invoke(cli, ["ui", "start", "--help"])
        assert result.exit_code == 0
        assert "--port" in result.output
        assert "--host" in result.output
        assert "--viewer-token" in result.output
        assert "--operator-token" in result.output
        assert "--agent-token" in result.output
        assert "--max-agents" in result.output

    def test_ui_start_default_port(self):
        """Verify default port is 8420."""
        result = runner.invoke(cli, ["ui", "start", "--help"])
        assert "8420" in result.output
