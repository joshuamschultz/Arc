"""working_dir decouple — the LLM's file/exec tools operate in the launch dir.

A coding agent operates its file/exec tools in the trusted project (working_dir) while
its own state stays in the workspace. This proves: working_dir() falls back to workspace
when unset; configure/snapshot/bind carry it (bind runs every turn); relative file paths
resolve against working_dir; and the sandbox boundary (workspace + allowed_paths) is
unchanged — working_dir never widens it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcagent.builtins.capabilities import _runtime


def test_working_dir_falls_back_to_workspace(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _runtime.configure(workspace=ws, tier="personal")
    assert _runtime.working_dir() == ws.resolve()


def test_working_dir_is_the_configured_launch_dir(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    proj = tmp_path / "proj"
    ws.mkdir()
    proj.mkdir()
    _runtime.configure(workspace=ws, allowed_paths=[proj], working_dir=proj, tier="personal")
    assert _runtime.working_dir() == proj.resolve()


def test_snapshot_bind_round_trips_working_dir(tmp_path: Path) -> None:
    # bind() runs at the top of every turn — if it dropped working_dir, turn 2 would
    # silently revert to the workspace. Guard that.
    ws = tmp_path / "ws"
    proj = tmp_path / "proj"
    ws.mkdir()
    proj.mkdir()
    _runtime.configure(workspace=ws, allowed_paths=[proj], working_dir=proj, tier="personal")
    snap = _runtime.snapshot()
    # Clobber the contextvar, then rebind (simulating a fresh sibling task).
    _runtime.configure(workspace=ws, tier="personal")
    assert _runtime.working_dir() == ws.resolve()
    _runtime.bind(snap)
    assert _runtime.working_dir() == proj.resolve()


def test_relative_paths_resolve_against_working_dir(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    proj = tmp_path / "proj"
    ws.mkdir()
    proj.mkdir()
    _runtime.configure(workspace=ws, allowed_paths=[proj], working_dir=proj, tier="personal")
    # A relative path lands in the project, not the workspace.
    resolved = _runtime.resolve_workspace_path("main.py", tool_name="write")
    assert resolved == (proj / "main.py").resolve()


def test_working_dir_does_not_widen_the_boundary(tmp_path: Path) -> None:
    # working_dir set to a project that is NOT in allowed_paths: a relative path there
    # still resolves against it, but the boundary check (workspace + allowed_paths) must
    # DENY it — working_dir moves the root, never the fence.
    ws = tmp_path / "ws"
    proj = tmp_path / "proj"
    ws.mkdir()
    proj.mkdir()
    _runtime.configure(workspace=ws, allowed_paths=None, working_dir=proj, tier="personal")
    with pytest.raises(Exception, match=r"outside workspace|not allowed|OUTSIDE"):
        _runtime.resolve_workspace_path("main.py", tool_name="write")
