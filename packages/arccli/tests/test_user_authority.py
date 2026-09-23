"""User CLI account operations require an injected custody-backed authority."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from arccli.commands.user import _store


def test_unconfigured_account_authority_is_explicitly_unavailable(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match="1"):
        _store(argparse.Namespace(file=None))
    assert "account authority is unavailable" in capsys.readouterr().err


def test_injected_account_authority_receives_requested_path(tmp_path: Path) -> None:
    seen: list[Path] = []
    wanted = tmp_path / "users.json"

    def factory(path: Path) -> object:
        seen.append(path)
        return object()

    store = _store(argparse.Namespace(file=str(wanted), user_store_factory=factory))
    assert store is not None
    assert seen == [wanted]
