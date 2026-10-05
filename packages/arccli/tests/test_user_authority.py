"""`arc user` account operations run through the Vault-backed authority."""

from __future__ import annotations

import argparse
import io
from pathlib import Path

import pytest
from packages.arccli.tests.accounts_support import enrolled_deployment

from arccli.commands.user import _store, user_handler


def test_unconfigured_account_authority_is_explicitly_unavailable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit, match="1"):
        _store(argparse.Namespace(file=None))
    assert "account authority is unavailable" in capsys.readouterr().err


def test_injected_account_authority_is_called_without_arguments() -> None:
    marker = object()
    assert _store(argparse.Namespace(user_store_factory=lambda: marker)) is marker


def test_user_commands_without_accounts_config_fail_closed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit, match="1"):
        user_handler(["list"])
    assert "account authority is unavailable" in capsys.readouterr().err


def test_user_add_and_list_run_against_the_enrolled_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with enrolled_deployment(tmp_path, monkeypatch) as deployment:
        monkeypatch.setattr("sys.stdin", io.StringIO("correct-horse-battery\n"))
        user_handler(
            ["--file", str(deployment.users_path), "add", "ada@example.com", "--password-stdin"]
        )
        capsys.readouterr()
        user_handler(["--file", str(deployment.users_path), "list"])
    out = capsys.readouterr().out
    assert "ada@example.com" in out
    assert "operator" in out
