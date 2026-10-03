"""Bare ``arc workflow ...`` must act on the OPERATOR root, where the runner lives.

Proven on production: with no ``--dir`` the CLI read ``~/.arc/state/workflows``
(the install home) while the runner and every real workflow live at
``~/arc/state/workflows``. ``arc workflow check`` reported "All 0 workflow(s)
readable" and exited 0 while eight workflows were unreadable.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust.paths import workflows_dir

from arccli.commands.workflow import _arc_dir, workflow_handler

_LEGACY = """[workflow]
schema_version = "1.0"
id = "morning"
version = 1
owner = "@olivia"

[trigger]
type = "cron"
expression = "0 7 * * *"

[[node]]
id = "a"
kind = "agent"
agent = "@olivia"
join = "all"
"""


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A bare user environment: HOME only, no ARC_TEAM_ROOT, no ARC_CONFIG_DIR."""
    monkeypatch.delenv("ARC_TEAM_ROOT", raising=False)
    monkeypatch.delenv("ARC_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def _run(*argv: str) -> int:
    try:
        workflow_handler(list(argv))
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def test_bare_check_finds_a_bundle_in_the_operator_root(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = home / "arc" / "state" / "workflows" / "morning"
    bundle.mkdir(parents=True)
    (bundle / "workflow.toml").write_text(_LEGACY, encoding="utf-8")

    assert _run("check") == 1
    out = capsys.readouterr()
    assert "morning" in out.out
    assert "1 of 1 workflow(s) cannot run as signed" in out.err


def test_bare_check_does_not_read_the_install_home(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = home / ".arc" / "state" / "workflows" / "decoy"
    bundle.mkdir(parents=True)
    (bundle / "workflow.toml").write_text(_LEGACY, encoding="utf-8")

    assert _run("check") == 0
    assert "All 0 workflow(s) readable." in capsys.readouterr().out


def test_cli_default_and_runner_default_resolve_the_same_workflows_path(home: Path) -> None:
    import argparse

    cli_root = workflows_dir(_arc_dir(argparse.Namespace(config_dir=None)))
    runner_root = workflows_dir()  # what RunnerHost / the fleet call, with no base

    assert cli_root == runner_root == home / "arc" / "state" / "workflows"


def test_gateway_connect_defaults_target_the_operator_config(home: Path) -> None:
    from arccli.commands.gateway_connect import _connect_paths

    gateway_config, env_file = _connect_paths(None, None)

    assert gateway_config == home / "arc" / "config" / "gateway.toml"
    assert env_file == home / "arc" / "config" / "arc.env"
    explicit = _connect_paths(str(home / "g.toml"), str(home / "e.env"))
    assert explicit == (home / "g.toml", home / "e.env")
