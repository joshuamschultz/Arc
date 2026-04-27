"""Smoke tests for arc run subcommands via subprocess.

These tests verify that each `arc run <subcommand>` invocation produces
expected output and exits correctly. They are the regression net for the
T1.1.5 migration.

NOTE: `arc run task` requires LLM API access — those tests are skipped
in this smoke suite. `arc run version` and `arc run exec` are safe.
"""

from __future__ import annotations

import json
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
# arc run (no subcommand — shows help)
# ---------------------------------------------------------------------------


class TestRunHelp:
    def test_no_args_exits_zero(self) -> None:
        """arc run with no args exits 0 and shows help."""
        result = _arc("run")
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_no_args_shows_subcommands(self) -> None:
        """arc run help lists subcommands."""
        combined = _arc("run").stdout + _arc("run").stderr
        assert any(sub in combined for sub in ["version", "exec", "task"])


# ---------------------------------------------------------------------------
# arc run version
# ---------------------------------------------------------------------------


class TestRunVersion:
    def test_version_exits_zero(self) -> None:
        """arc run version exits 0."""
        result = _arc("run", "version")
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_version_output_nonempty(self) -> None:
        """arc run version produces non-empty stdout."""
        result = _arc("run", "version")
        assert result.stdout.strip()

    def test_version_shows_arcrun(self) -> None:
        """arc run version shows arcrun version."""
        result = _arc("run", "version")
        assert "arcrun" in result.stdout

    def test_version_shows_strategies(self) -> None:
        """arc run version shows available strategies."""
        result = _arc("run", "version")
        assert "strategies" in result.stdout.lower() or "react" in result.stdout.lower()

    def test_version_json(self) -> None:
        """arc run version --json produces valid JSON."""
        result = _arc("run", "version", "--json")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert "arcrun" in data
        assert "strategies" in data
        assert isinstance(data["strategies"], list)


# ---------------------------------------------------------------------------
# arc run exec
# ---------------------------------------------------------------------------


class TestRunExec:
    def test_exec_simple_print(self) -> None:
        """arc run exec 'print(42)' exits 0 and prints 42."""
        result = _arc("run", "exec", "print(42)")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "42" in result.stdout

    def test_exec_output_nonempty(self) -> None:
        """arc run exec produces non-empty stdout."""
        result = _arc("run", "exec", "print('hello')")
        assert result.stdout.strip()

    def test_exec_json(self) -> None:
        """arc run exec --json produces valid JSON."""
        result = _arc("run", "exec", "print('hi')", "--json")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert "stdout" in data

    def test_exec_math_expression(self) -> None:
        """arc run exec can run a math expression."""
        result = _arc("run", "exec", "print(2 + 2)")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "4" in result.stdout

    def test_exec_multiline(self) -> None:
        """arc run exec handles multi-statement code."""
        result = _arc("run", "exec", "x = 10; y = 20; print(x + y)")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "30" in result.stdout


# Mark to avoid unused import warning
_ = pytest
