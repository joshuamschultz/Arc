"""Tests for `arc team backfill-workspaces` (SPEC-019 T1.4, FR-3).

Behavior matrix:
  - --dry-run (default): proposes changes, writes nothing
  - --apply: writes workspace_path on matching entities
  - second --apply on same state is a no-op (idempotent)
  - missing arcagent.toml: skipped
  - malformed arcagent.toml: skipped with warning, does not abort
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

import pytest

from arccli.commands.team import _backfill_workspaces, _init_cmd, _register


def _init_root(tmp_path: Path) -> Path:
    args = argparse.Namespace(root_path=str(tmp_path))
    _init_cmd(args)
    return tmp_path


def _register_agent(root: Path, entity_id: str, name: str) -> None:
    """Register agent with no workspace_path (simulating legacy)."""
    args = argparse.Namespace(
        root=str(root),
        entity_id=entity_id,
        name=name,
        entity_type="user",  # use user to skip workspace requirement
        roles="",
        workspace=None,
    )
    _register(args)


def _make_team_dir(
    team_dir: Path, agent_name: str, workspace_subpath: str = "./workspace"
) -> Path:
    """Create a `team/<agent_name>/arcagent.toml` and an inner workspace dir.

    Returns the absolute resolved workspace path the backfill should record.
    """
    agent_dir = team_dir / agent_name
    agent_dir.mkdir(parents=True, exist_ok=True)
    toml = f'''[agent]
name = "user://{agent_name}"
workspace = "{workspace_subpath}"
'''
    (agent_dir / "arcagent.toml").write_text(toml)
    workspace = (agent_dir / workspace_subpath).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def _backfill_args(root: Path, team_dir: Path, *, apply: bool) -> argparse.Namespace:
    return argparse.Namespace(
        root=str(root),
        team_dir=str(team_dir),
        apply=apply,
    )


def _read_workspace_path(backend: Any, entity_id: str) -> str | None:
    """Read workspace_path for the entity addressed by ``entity_id``.

    Records are DID-keyed in the arcteam registry collection, so match on
    handle (the URI local part).
    """
    handle = entity_id.split("://")[-1]
    for record in asyncio.run(backend.query("messages/registry")):
        if record.get("handle") == handle:
            return record.get("workspace_path")
    return None


class TestBackfillDryRun:
    """Default mode: dry-run reports changes but writes nothing."""

    def test_dry_run_does_not_persist(self, tmp_path: Path, team_backend: Any) -> None:
        root = _init_root(tmp_path)
        _register_agent(root, "user://a1", "A1")

        team_dir = tmp_path / "team"
        _make_team_dir(team_dir, "a1")

        args = _backfill_args(root, team_dir, apply=False)
        _backfill_workspaces(args)

        assert _read_workspace_path(team_backend, "user://a1") is None


class TestBackfillApply:
    """--apply writes workspace_path."""

    def test_apply_writes_workspace_path(self, tmp_path: Path, team_backend: Any) -> None:
        root = _init_root(tmp_path)
        _register_agent(root, "user://a1", "A1")

        team_dir = tmp_path / "team"
        expected = _make_team_dir(team_dir, "a1")

        args = _backfill_args(root, team_dir, apply=True)
        _backfill_workspaces(args)

        assert _read_workspace_path(team_backend, "user://a1") == str(expected)


class TestBackfillIdempotent:
    """Second --apply with no changes is a no-op."""

    def test_second_apply_no_op(
        self, tmp_path: Path, team_backend: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = _init_root(tmp_path)
        _register_agent(root, "user://a1", "A1")
        team_dir = tmp_path / "team"
        _make_team_dir(team_dir, "a1")

        args = _backfill_args(root, team_dir, apply=True)
        _backfill_workspaces(args)
        capsys.readouterr()
        _backfill_workspaces(args)
        out = capsys.readouterr().out
        assert "unchanged" in out


class TestBackfillSkipsMissing:
    """Missing arcagent.toml is silently skipped."""

    def test_missing_toml_skipped(self, tmp_path: Path, team_backend: Any) -> None:
        root = _init_root(tmp_path)
        _register_agent(root, "user://a1", "A1")
        team_dir = tmp_path / "team"
        # No arcagent.toml created
        team_dir.mkdir()

        args = _backfill_args(root, team_dir, apply=True)
        _backfill_workspaces(args)

        # No write occurred
        assert _read_workspace_path(team_backend, "user://a1") is None


class TestBackfillMalformedToml:
    """Malformed TOML produces warning, does not abort."""

    def test_malformed_toml_skipped_with_warning(
        self, tmp_path: Path, team_backend: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = _init_root(tmp_path)
        _register_agent(root, "user://a1", "A1")
        team_dir = tmp_path / "team"
        agent_dir = team_dir / "a1"
        agent_dir.mkdir(parents=True)
        (agent_dir / "arcagent.toml").write_text("not [valid toml")

        args = _backfill_args(root, team_dir, apply=True)
        _backfill_workspaces(args)
        out = capsys.readouterr().out
        assert "skip" in out.lower()
        assert _read_workspace_path(team_backend, "user://a1") is None
