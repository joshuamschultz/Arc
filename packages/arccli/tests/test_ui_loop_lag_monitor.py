"""``arc ui start`` runs the loop-lag monitor only when ``[ui]`` opts in."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from arccli.commands.ui import _loop_lag_monitor_enabled, _maybe_build_gateway_config


def _args() -> argparse.Namespace:
    return argparse.Namespace(gateway_config=None, no_chat=False)


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path))
    (tmp_path / "config").mkdir()
    return tmp_path / "config"


def test_default_start_reads_the_opt_in_from_gateway_toml(config_dir: Path) -> None:
    (config_dir / "gateway.toml").write_text("[ui]\nloop_lag_monitor = true\n")
    assert _loop_lag_monitor_enabled(_maybe_build_gateway_config(_args(), Path.cwd()))


def test_monitor_is_off_without_the_opt_in(config_dir: Path) -> None:
    assert not _loop_lag_monitor_enabled(_maybe_build_gateway_config(_args(), Path.cwd()))
