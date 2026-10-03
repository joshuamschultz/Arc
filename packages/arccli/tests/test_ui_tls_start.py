"""J1-2 — ``arc ui start`` serves https from the certificate saved in Settings → Access.

The start opens the key through custody and hands uvicorn the encrypted key and
its passphrase. Federal never serves plain http off loopback.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from packages.arcui.tests.ui_settings_support import isolate, self_signed, set_tier

from arccli.commands.ui import BOOTSTRAP_HASH_KEY, _start


def _args(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "host": "127.0.0.1",
        "port": 18420,
        "viewer_token": "v",
        "operator_token": "o",
        "max_agents": 10,
        "show_tokens": False,
        "root": None,
        "no_chat": True,
        "no_browser": True,
        "team_root": None,
        "gateway_config": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ARCSTORE_DATABASE_URL", "postgresql://arc:test@127.0.0.1/arc")
    monkeypatch.chdir(tmp_path)
    return isolate(tmp_path, monkeypatch)


def _run(args: argparse.Namespace) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    class _SpyServer:
        def __init__(self, server_config: Any) -> None:
            captured["config"] = server_config

        def run(self) -> None:
            return None

    with patch("uvicorn.Server", _SpyServer):
        _start(args)
    return captured


def test_saved_certificate_is_served(config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from arcui.ui_tls import save_tls

    cert, key = self_signed()
    save_tls(cert, key)
    server = _run(_args())["config"]
    assert server.ssl_certfile == str(config / "ui-tls.cert.pem")
    assert server.ssl_keyfile == str(config / "ui-tls.key.pem")
    assert server.ssl_keyfile_password
    assert server.app.state.ui_tls_active is True
    out = capsys.readouterr().out
    assert f"https://127.0.0.1:18420/#{BOOTSTRAP_HASH_KEY}=v" in out
    assert server.ssl_keyfile_password not in out


def test_no_certificate_serves_plain_http(config: Path) -> None:
    server = _run(_args())["config"]
    assert server.ssl_certfile is None
    assert server.app.state.ui_tls_active is False


def test_a_key_custody_can_not_open_stops_the_start(config: Path) -> None:
    from arcui.ui_tls import save_tls

    cert, key = self_signed()
    save_tls(cert, key)
    (config / "ui-tls.key.sealed").write_text("xc1:tampered")
    with pytest.raises(SystemExit):
        _run(_args())


def test_federal_refuses_plain_http_off_loopback(config: Path) -> None:
    set_tier(config, fleet="federal")
    with (
        patch("arcui.create_app") as create_app,
        patch("uvicorn.Server"),
        pytest.raises(SystemExit),
    ):
        _start(_args(host="0.0.0.0"))  # noqa: S104 - the bind under test is the off-loopback one
    create_app.assert_not_called()


def test_federal_loopback_behind_a_proxy_may_serve_http(config: Path) -> None:
    set_tier(config, fleet="federal")
    from arccli.commands.ui import _serving_tls

    assert _serving_tls("127.0.0.1") is None
