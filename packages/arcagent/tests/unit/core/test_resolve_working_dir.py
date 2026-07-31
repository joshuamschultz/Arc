"""agent_lifecycle._resolve_working_dir — opt-in + trust gate for the launch dir.

The launch dir (ARC_WORKING_DIR) is honored only when the agent opts in
(tools.operate_in_launch_dir) AND the dir is already inside workspace + allowed_paths
(the folder-trust prompt is what puts it there). Otherwise the tools stay workspace-rooted.
This never widens the sandbox.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from arcagent.core.agent_lifecycle import _resolve_working_dir


def _config(*, opt_in: bool) -> SimpleNamespace:
    # _resolve_working_dir only reads config.tools.operate_in_launch_dir.
    return SimpleNamespace(tools=SimpleNamespace(operate_in_launch_dir=opt_in))


def test_flag_off_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_WORKING_DIR", str(tmp_path))
    assert _resolve_working_dir(_config(opt_in=False), tmp_path / "ws", [tmp_path]) is None


def test_no_env_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARC_WORKING_DIR", raising=False)
    assert _resolve_working_dir(_config(opt_in=True), tmp_path / "ws", None) is None


def test_env_within_allowed_is_honored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("ARC_WORKING_DIR", str(proj))
    # proj is trusted (in allowed_paths) → honored.
    got = _resolve_working_dir(_config(opt_in=True), tmp_path / "ws", [proj])
    assert got == proj.resolve()


def test_env_outside_allowed_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    untrusted = tmp_path / "untrusted"
    untrusted.mkdir()
    monkeypatch.setenv("ARC_WORKING_DIR", str(untrusted))
    # Not in workspace or allowed_paths → refused (sandbox floor), tools stay in workspace.
    assert (
        _resolve_working_dir(_config(opt_in=True), tmp_path / "ws", [tmp_path / "other"]) is None
    )
