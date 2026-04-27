"""Tests for arc ui subcommands — subprocess-based (T1.1.5 migration)."""

from __future__ import annotations

import subprocess
from pathlib import Path

_ARC = Path(__file__).parent.parent.parent.parent / ".venv" / "bin" / "arc"


def _arc(*args: str) -> subprocess.CompletedProcess[str]:
    """Run `arc <args>` and return the CompletedProcess."""
    return subprocess.run(
        [str(_ARC), *args],
        capture_output=True,
        text=True,
    )


class TestUIGroup:
    def test_ui_help(self):
        result = _arc("ui", "--help")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "start" in result.stdout

    def test_ui_start_help(self):
        result = _arc("ui", "start", "--help")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "--port" in result.stdout
        assert "--host" in result.stdout
        assert "--viewer-token" in result.stdout
        assert "--operator-token" in result.stdout
        assert "--agent-token" in result.stdout
        assert "--max-agents" in result.stdout

    def test_ui_start_default_port(self):
        """Verify default port 8420 is mentioned in help."""
        result = _arc("ui", "start", "--help")
        assert "8420" in result.stdout
