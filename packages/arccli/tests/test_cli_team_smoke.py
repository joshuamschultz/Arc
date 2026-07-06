"""Smoke tests for arc team subcommands via subprocess.

These tests verify that each `arc team <subcommand>` invocation produces
expected output and exits correctly. They are the regression net for the
T1.1.5 migration.

Team commands that require an initialized team data directory use a
tmp_path fixture to isolate state.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from arccli.commands.team import _entities, _init_cmd, _register, _status

_ARC = Path(__file__).parent.parent.parent.parent / ".venv" / "bin" / "arc"


def _arc(*args: str) -> subprocess.CompletedProcess[str]:
    """Run `arc <args>` and return the CompletedProcess."""
    return subprocess.run(
        [str(_ARC), *args],
        capture_output=True,
        text=True,
    )


def _init_team(tmp_path: Path) -> None:
    """Initialize a team data directory for tests that need it."""
    result = _arc("team", "init", "--root", str(tmp_path))
    assert result.returncode == 0, f"team init failed: {result.stderr}"


# ---------------------------------------------------------------------------
# arc team (no subcommand — shows help)
# ---------------------------------------------------------------------------


class TestTeamHelp:
    def test_no_args_exits_zero(self) -> None:
        """arc team with no args exits 0 and shows help."""
        result = _arc("team")
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_no_args_shows_subcommands(self) -> None:
        """arc team help lists expected subcommands."""
        result = _arc("team")
        combined = result.stdout + result.stderr
        assert any(sub in combined for sub in ["status", "init", "config", "entities"])


# ---------------------------------------------------------------------------
# arc team init
# ---------------------------------------------------------------------------


class TestTeamInit:
    def test_init_exits_zero(self, tmp_path: Path) -> None:
        """arc team init exits 0."""
        result = _arc("team", "init", "--root", str(tmp_path))
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_init_output_nonempty(self, tmp_path: Path) -> None:
        """arc team init produces non-empty stdout."""
        result = _arc("team", "init", "--root", str(tmp_path))
        assert result.stdout.strip()

    def test_init_creates_directories(self, tmp_path: Path) -> None:
        """arc team init creates the directories that arcteam actually uses.

        Paths must mirror the collection layout in arcteam.registry,
        arcteam.messenger, and arcteam.audit so `status` reads where
        registration writes.
        """
        _arc("team", "init", "--root", str(tmp_path))
        assert (tmp_path / "messages" / "registry").is_dir()
        assert (tmp_path / "messages" / "channels").is_dir()
        assert (tmp_path / "messages" / "cursors").is_dir()
        assert (tmp_path / "messages" / "streams").is_dir()
        assert (tmp_path / "audit" / "audit").is_dir()

    def test_init_creates_hmac_key(self, tmp_path: Path) -> None:
        """arc team init creates .hmac_key file."""
        _arc("team", "init", "--root", str(tmp_path))
        assert (tmp_path / ".hmac_key").exists()


# ---------------------------------------------------------------------------
# arc team status
# ---------------------------------------------------------------------------


class TestTeamStatus:
    """In-process: ``status`` now reads live counts from the arcteam backend,
    so these tests inject an in-memory backend (``team_backend``) rather than
    depending on a NATS server."""

    def test_status_exits_zero(
        self, tmp_path: Path, team_backend: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _init_cmd(argparse.Namespace(root_path=str(tmp_path)))
        capsys.readouterr()
        _status(argparse.Namespace(root=str(tmp_path), use_json=False))
        assert capsys.readouterr().out.strip()

    def test_status_shows_root(
        self, tmp_path: Path, team_backend: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _init_cmd(argparse.Namespace(root_path=str(tmp_path)))
        capsys.readouterr()
        _status(argparse.Namespace(root=str(tmp_path), use_json=False))
        assert str(tmp_path) in capsys.readouterr().out

    def test_status_json(
        self, tmp_path: Path, team_backend: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _init_cmd(argparse.Namespace(root_path=str(tmp_path)))
        capsys.readouterr()
        _status(argparse.Namespace(root=str(tmp_path), use_json=True))
        data = json.loads(capsys.readouterr().out)
        assert "root" in data
        assert "entities" in data

    def test_status_counts_registered_entities(
        self, tmp_path: Path, team_backend: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """status entity count reflects entities written by `register`."""
        _init_cmd(argparse.Namespace(root_path=str(tmp_path)))
        _register(
            argparse.Namespace(
                root=str(tmp_path),
                entity_id="agent_one",
                name="Agent One",
                entity_type="agent",
                roles="",
                workspace=None,
            )
        )
        capsys.readouterr()
        _status(argparse.Namespace(root=str(tmp_path), use_json=True))
        data = json.loads(capsys.readouterr().out)
        assert data["entities"] == 1, f"expected entities=1, got {data}"


# ---------------------------------------------------------------------------
# arc team config
# ---------------------------------------------------------------------------


class TestTeamConfig:
    def test_config_exits_zero(self) -> None:
        """arc team config exits 0."""
        result = _arc("team", "config")
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_config_output_nonempty(self) -> None:
        """arc team config produces non-empty stdout."""
        result = _arc("team", "config")
        assert result.stdout.strip()

    def test_config_json(self) -> None:
        """arc team config --json produces valid JSON."""
        result = _arc("team", "config", "--json")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert "root" in data


# ---------------------------------------------------------------------------
# arc team entities
# ---------------------------------------------------------------------------


class TestTeamEntities:
    """In-process: ``entities`` lists from the arcteam backend (injected here)."""

    def test_entities_exits_zero(
        self, tmp_path: Path, team_backend: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _init_cmd(argparse.Namespace(root_path=str(tmp_path)))
        capsys.readouterr()
        _entities(argparse.Namespace(root=str(tmp_path), role=None, use_json=False))
        assert capsys.readouterr().out.strip()

    def test_entities_output_nonempty(
        self, tmp_path: Path, team_backend: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _init_cmd(argparse.Namespace(root_path=str(tmp_path)))
        capsys.readouterr()
        _entities(argparse.Namespace(root=str(tmp_path), role=None, use_json=False))
        # Empty registry prints "No entities registered." — still non-empty output.
        assert capsys.readouterr().out.strip()


# Mark to avoid unused import warning
_ = pytest
