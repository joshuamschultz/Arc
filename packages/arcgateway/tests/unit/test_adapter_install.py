"""Tests for the operator adapter-install helper (arcgateway.adapters.install).

Builds the pip/uv command for an official adapter extension package and runs
it through an injectable runner — no real installs in the test suite.
"""

from __future__ import annotations

import sys

import pytest

from arcgateway.adapters.install import (
    UnknownAdapterError,
    available_adapters,
    build_install_command,
    install_adapter,
)


def test_available_adapters_lists_official() -> None:
    avail = available_adapters()
    assert avail["telegram"] == "arcgateway-telegram"
    assert avail["slack"] == "arcgateway-slack"
    assert avail["mattermost"] == "arcgateway-mattermost"


def test_build_command_with_uv() -> None:
    cmd = build_install_command("telegram", prefer_uv=True)
    assert cmd == ["uv", "pip", "install", "arcgateway-telegram"]


def test_build_command_with_pip() -> None:
    cmd = build_install_command("slack", prefer_uv=False)
    assert cmd == [sys.executable, "-m", "pip", "install", "arcgateway-slack"]


def test_build_command_upgrade_flag() -> None:
    cmd = build_install_command("telegram", upgrade=True, prefer_uv=True)
    assert cmd == ["uv", "pip", "install", "--upgrade", "arcgateway-telegram"]


def test_build_command_rejects_unknown_adapter() -> None:
    with pytest.raises(UnknownAdapterError):
        build_install_command("discord", prefer_uv=True)


def test_build_command_rejects_invalid_name() -> None:
    with pytest.raises(ValueError):
        build_install_command("../evil", prefer_uv=True)


def test_install_adapter_runs_command_and_returns_code() -> None:
    calls: list[list[str]] = []

    class _Proc:
        returncode = 0

    def _runner(cmd: list[str]) -> _Proc:
        calls.append(cmd)
        return _Proc()

    rc = install_adapter("telegram", prefer_uv=False, runner=_runner)
    assert rc == 0
    assert calls == [[sys.executable, "-m", "pip", "install", "arcgateway-telegram"]]


def test_install_adapter_propagates_nonzero_exit() -> None:
    class _Proc:
        returncode = 7

    rc = install_adapter("slack", prefer_uv=True, runner=lambda cmd: _Proc())
    assert rc == 7
