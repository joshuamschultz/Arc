"""Tests for `arc team register --workspace` (SPEC-019 T1.3, FR-1).

Validates:
  - default = Path.cwd() resolved to absolute
  - explicit path resolved to absolute
  - non-existent path rejected
  - file (not directory) rejected
  - ~ or env vars in raw input rejected (no late-binding per SR-6)
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

from arccli.commands.team import _register


def _init_root(tmp_path: Path) -> Path:
    """Bootstrap a minimal team data root."""
    from arccli.commands.team import _init_cmd

    args = argparse.Namespace(root_path=str(tmp_path))
    _init_cmd(args)
    return tmp_path


def _register_args(
    root: Path,
    workspace: str | None,
    entity_id: str = "agent://w1",
    name: str = "W1",
) -> argparse.Namespace:
    return argparse.Namespace(
        root=str(root),
        entity_id=entity_id,
        name=name,
        entity_type="agent",
        roles="",
        workspace=workspace,
    )


class TestRegisterWorkspace:
    """SPEC-019 T1.3."""

    def test_explicit_workspace_resolves_to_absolute(self, tmp_path: Path) -> None:
        root = _init_root(tmp_path)
        ws = tmp_path / "workspace_a"
        ws.mkdir()
        args = _register_args(root, str(ws))
        _register(args)

        # Check stored entity
        from arcteam.config import TeamConfig
        from arcteam.storage import FileBackend
        backend = FileBackend(root)

        import asyncio
        record = asyncio.run(
            backend.read("messages/registry", "agent_w1")
        )
        assert record is not None
        assert record["workspace_path"] == str(ws.resolve())

    def test_default_workspace_is_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = _init_root(tmp_path)
        cwd_dir = tmp_path / "cwd_subdir"
        cwd_dir.mkdir()
        monkeypatch.chdir(cwd_dir)

        args = _register_args(root, workspace=None)
        _register(args)

        from arcteam.storage import FileBackend
        backend = FileBackend(root)

        import asyncio
        record = asyncio.run(
            backend.read("messages/registry", "agent_w1")
        )
        assert record is not None
        assert record["workspace_path"] == str(cwd_dir.resolve())

    def test_nonexistent_workspace_rejected(self, tmp_path: Path) -> None:
        root = _init_root(tmp_path)
        bogus = tmp_path / "does_not_exist"
        args = _register_args(root, str(bogus))
        with pytest.raises((SystemExit, ValueError)):
            _register(args)

    def test_file_workspace_rejected(self, tmp_path: Path) -> None:
        root = _init_root(tmp_path)
        f = tmp_path / "file.txt"
        f.write_text("x")
        args = _register_args(root, str(f))
        with pytest.raises((SystemExit, ValueError)):
            _register(args)

    def test_tilde_in_raw_input_rejected(self, tmp_path: Path) -> None:
        root = _init_root(tmp_path)
        # ~ is rejected to enforce SR-6 (no late-binding shorthand persisted).
        # The home expansion would happen at use time, which is too late.
        args = _register_args(root, "~/some/path")
        with pytest.raises((SystemExit, ValueError)):
            _register(args)

    def test_envvar_in_raw_input_rejected(self, tmp_path: Path) -> None:
        root = _init_root(tmp_path)
        args = _register_args(root, "$HOME/path")
        with pytest.raises((SystemExit, ValueError)):
            _register(args)
