"""`arc ui start` hands the dashboard the Vault-backed account store (People tab)."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from arcstore.backends.memory import FakeBackend
from arctrust.paths import config_file
from packages.arccli.tests.accounts_support import enrolled_deployment


@pytest.fixture(autouse=True)
def _isolated_arc_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARC_TEAM_ROOT", raising=False)
    monkeypatch.delenv("ARCSTORE_DATA_DIR", raising=False)
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-home"))


def _ui_args() -> argparse.Namespace:
    return argparse.Namespace(
        host="127.0.0.1",
        port=18422,
        viewer_token="viewer",
        operator_token="operator",
        max_agents=10,
        show_tokens=False,
        root=None,
        no_browser=True,
        no_chat=True,
        team_root=None,
    )


def _start_ui(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, dict[str, Any]]:
    import arcui

    from arccli.commands.ui import _start

    monkeypatch.setenv("ARCSTORE_DATABASE_URL", "postgresql://arc:test@127.0.0.1/arc")
    real_create_app = arcui.create_app
    captured: dict[str, Any] = {}

    def _create_app(**kwargs: Any) -> Any:
        captured.update(kwargs)
        captured["app"] = real_create_app(**kwargs, arcstore_backend=FakeBackend())
        return captured["app"]

    monkeypatch.setattr(arcui, "create_app", _create_app)

    class _NoServe:
        def __init__(self, config: Any) -> None:
            self.app = config.app

        def run(self) -> None:
            return None

    with patch("uvicorn.Server", _NoServe):
        _start(_ui_args())
    return captured["app"], captured


def test_ui_start_passes_the_account_factory_when_accounts_are_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with enrolled_deployment(tmp_path, monkeypatch):
        app, kwargs = _start_ui(monkeypatch)
    assert kwargs["user_store_factory"] is not None
    assert app.state.user_store_factory is kwargs["user_store_factory"]


def test_ui_start_without_accounts_leaves_people_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file("arcagent.toml").parent.mkdir(parents=True, exist_ok=True)
    config_file("arcagent.toml").write_text('[security]\ntier = "personal"\n')
    app, kwargs = _start_ui(monkeypatch)
    assert kwargs["user_store_factory"] is None
    assert app.state.user_store_factory is None


def test_ui_start_survives_a_broken_accounts_block(monkeypatch: pytest.MonkeyPatch) -> None:
    config_file("arcagent.toml").parent.mkdir(parents=True, exist_ok=True)
    config_file("arcagent.toml").write_text('[security.accounts]\nvault_url = "http://x"\n')
    _, kwargs = _start_ui(monkeypatch)
    assert kwargs["user_store_factory"] is None
