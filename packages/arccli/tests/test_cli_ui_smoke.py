"""Smoke tests for arc ui subcommands via subprocess.

These tests verify that each `arc ui <subcommand>` invocation produces
expected output and exits correctly. They are the regression net for the
T1.1.5 migration.

NOTE: `arc ui start` actually starts a server, so we only test --help.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_ARC = Path(__file__).parent.parent.parent.parent / ".venv" / "bin" / "arc"


def _arc(*args: str) -> subprocess.CompletedProcess[str]:
    """Run `arc <args>` and return the CompletedProcess."""
    return subprocess.run(
        [str(_ARC), *args],
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# arc ui (no subcommand — shows help)
# ---------------------------------------------------------------------------


class TestUiHelp:
    def test_no_args_exits_zero(self) -> None:
        """arc ui with no args exits 0 and shows help."""
        result = _arc("ui")
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_no_args_shows_start(self) -> None:
        """arc ui help lists the start subcommand."""
        result = _arc("ui")
        combined = result.stdout + result.stderr
        assert "start" in combined

    def test_help_flag(self) -> None:
        """arc ui --help exits 0."""
        result = _arc("ui", "--help")
        assert result.returncode == 0, f"stderr: {result.stderr}"


# ---------------------------------------------------------------------------
# arc ui start --help (safe — does not start server)
# ---------------------------------------------------------------------------


class TestUiStartHelp:
    def test_start_help_exits_zero(self) -> None:
        """arc ui start --help exits 0."""
        result = _arc("ui", "start", "--help")
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_start_help_shows_port(self) -> None:
        """arc ui start --help mentions --port option."""
        result = _arc("ui", "start", "--help")
        assert "--port" in result.stdout

    def test_start_help_shows_host(self) -> None:
        """arc ui start --help mentions --host option."""
        result = _arc("ui", "start", "--help")
        assert "--host" in result.stdout

    def test_start_help_shows_viewer_token(self) -> None:
        """arc ui start --help mentions --viewer-token option."""
        result = _arc("ui", "start", "--help")
        assert "--viewer-token" in result.stdout

    def test_start_help_shows_operator_token(self) -> None:
        """arc ui start --help mentions --operator-token option."""
        result = _arc("ui", "start", "--help")
        assert "--operator-token" in result.stdout

    def test_start_help_shows_agent_token(self) -> None:
        """arc ui start --help mentions --agent-token option."""
        result = _arc("ui", "start", "--help")
        assert "--agent-token" in result.stdout

    def test_start_help_shows_max_agents(self) -> None:
        """arc ui start --help mentions --max-agents option."""
        result = _arc("ui", "start", "--help")
        assert "--max-agents" in result.stdout

    def test_start_help_shows_default_port(self) -> None:
        """arc ui start --help shows the default port (8420)."""
        result = _arc("ui", "start", "--help")
        assert "8420" in result.stdout


# Mark to avoid unused import warning
_ = pytest
