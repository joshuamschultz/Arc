"""A chat-platform outage at boot degrades that platform; the dashboard still starts.

Regression for the 2.5 h overnight outage: with the host's network down,
Telegram init raised out of the arcui lifespan ("Application startup failed.
Exiting.") and systemd restarted the whole stack into the same failure.

Real path: ``create_app`` → lifespan → ``build_for_embedded`` → registry builds
the real ``TelegramAdapter`` → its connect() meets a network that stays down.
Only python-telegram-bot's ``Application`` is faked.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from arcgateway.adapters.telegram import adapter as telegram_adapter
from arcgateway.config import GatewayConfig
from arcstore.backends.memory import FakeBackend
from arctrust.paths import ARC_CONFIG_DIR_ENV, config_file
from starlette.testclient import TestClient

from arcui.auth import AuthConfig
from arcui.server import create_app

_GATEWAY_TOML = """
[gateway]
agent_did = "did:arc:agent:default"

[platforms.web]
enabled = true

[platforms.telegram]
enabled = true
token_env = "ARC_TEST_TELEGRAM_TOKEN"
allowed_user_ids = [42]
"""


class NetworkError(Exception):
    """Named like python-telegram-bot's ``telegram.error.NetworkError``."""


class InvalidTokenError(Exception):
    """Named like python-telegram-bot's ``telegram.error.InvalidToken``."""


def _install_fake_telegram(monkeypatch: pytest.MonkeyPatch, init_error: Exception) -> MagicMock:
    app = MagicMock()
    app.initialize = AsyncMock(side_effect=init_error)
    app.shutdown = AsyncMock()
    app.stop = AsyncMock()
    app.updater = MagicMock()
    app.updater.running = False
    builder = MagicMock()
    builder.token.return_value = builder
    builder.concurrent_updates.return_value = builder
    builder.build.return_value = app
    fake_ext = MagicMock()
    fake_ext.Application.builder.return_value = builder
    monkeypatch.setitem(sys.modules, "telegram.ext", fake_ext)
    monkeypatch.setattr(telegram_adapter, "_network_backoff", lambda _attempt: 0.01)
    return app


@pytest.fixture
def gateway_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GatewayConfig:
    monkeypatch.setenv(ARC_CONFIG_DIR_ENV, str(tmp_path / "arc-home"))
    monkeypatch.setenv("ARC_TEST_TELEGRAM_TOKEN", "123:test-token")
    path = config_file("gateway.toml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_GATEWAY_TOML, encoding="utf-8")
    return GatewayConfig.from_toml_str(_GATEWAY_TOML)


def _app(tmp_path: Path, gateway_config: GatewayConfig) -> object:
    team_root = tmp_path / "team"
    team_root.mkdir()
    return create_app(
        team_root=team_root,
        gateway_config=gateway_config,
        arcstore_backend=FakeBackend(),
        auth_config=AuthConfig({"viewer_token": "viewer-tok"}),
    )


def test_startup_completes_and_reports_telegram_degraded(
    tmp_path: Path, gateway_config: GatewayConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_telegram(monkeypatch, NetworkError("network is unreachable"))

    with TestClient(_app(tmp_path, gateway_config)) as client:
        assert client.get("/api/health").status_code == 200
        resp = client.get("/api/team/gateways", headers={"Authorization": "Bearer viewer-tok"})

    assert resp.status_code == 200
    [telegram] = resp.json()["gateways"]
    assert telegram["name"] == "telegram"
    assert telegram["status"] == "degraded"
    assert "network is unreachable" in telegram["last_error"]


def test_bad_token_still_fails_startup(
    tmp_path: Path, gateway_config: GatewayConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_telegram(monkeypatch, InvalidTokenError("The token was rejected by the server."))

    with pytest.raises(InvalidTokenError), TestClient(_app(tmp_path, gateway_config)):
        pass
