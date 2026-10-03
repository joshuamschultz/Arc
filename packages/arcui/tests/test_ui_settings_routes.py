"""J1-2 — Settings → Access: the public address and the dashboard's own https certificate.

A customer whose browser is not on the Arc host must be able to finish an OAuth
sign-in with no terminal. These tests drive the two Settings routes the way the
page does, then read back what the next sign-in will use.
"""

from __future__ import annotations

import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from packages.arcui.tests.ui_settings_support import (
    OPERATOR,
    VIEWER,
    isolate,
    self_signed,
    set_tier,
    settings_client,
)

LOOPBACK_REDIRECT = "http://127.0.0.1:8420/oauth/callback"


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return isolate(tmp_path, monkeypatch)


# --- public address ---------------------------------------------------------


def test_unset_address_answers_the_loopback_redirect(config: Path) -> None:
    client, _ = settings_client()
    body = client.get("/api/settings/public-address", headers=VIEWER).json()
    assert body["public_base_url"] is None
    assert body["redirect_uri"] == LOOPBACK_REDIRECT
    assert body["tier"] == "personal"
    assert body["https_required"] is False


def test_saved_address_drives_the_redirect_with_no_restart(config: Path) -> None:
    client, audit = settings_client()
    answer = client.put(
        "/api/settings/public-address",
        json={"public_base_url": "https://arc.example.ts.net/"},
        headers=OPERATOR,
    )
    assert answer.status_code == 200
    assert answer.json()["public_base_url"] == "https://arc.example.ts.net"
    assert answer.json()["redirect_uri"] == "https://arc.example.ts.net/oauth/callback"
    stored = tomllib.loads((config / "gateway.toml").read_text())
    assert stored["ui"]["public_base_url"] == "https://arc.example.ts.net"
    assert client.app.state.public_address.redirect_uri() == (
        "https://arc.example.ts.net/oauth/callback"
    )
    assert audit.events[-1]["operation"] == "ui.public_address.write"
    assert audit.events[-1]["outcome"] == "applied"


def test_saving_keeps_the_rest_of_gateway_toml(config: Path) -> None:
    (config / "gateway.toml").write_text(
        '# operator note\n[gateway]\ntier = "personal"\n\n[platforms.web]\nenabled = true\n'
    )
    client, _ = settings_client()
    client.put(
        "/api/settings/public-address",
        json={"public_base_url": "https://arc.example.com"},
        headers=OPERATOR,
    )
    text = (config / "gateway.toml").read_text()
    assert "# operator note" in text
    assert tomllib.loads(text)["platforms"]["web"]["enabled"] is True


def test_clearing_returns_to_loopback(config: Path) -> None:
    client, _ = settings_client()
    client.put(
        "/api/settings/public-address",
        json={"public_base_url": "https://arc.example.com"},
        headers=OPERATOR,
    )
    answer = client.put(
        "/api/settings/public-address", json={"public_base_url": None}, headers=OPERATOR
    )
    assert answer.json()["public_base_url"] is None
    assert answer.json()["redirect_uri"] == LOOPBACK_REDIRECT


def test_viewer_cannot_change_the_address(config: Path) -> None:
    client, _ = settings_client()
    answer = client.put(
        "/api/settings/public-address",
        json={"public_base_url": "https://arc.example.com"},
        headers=VIEWER,
    )
    assert answer.status_code == 403
    assert not (config / "gateway.toml").exists()


def test_http_loopback_is_allowed_at_personal(config: Path) -> None:
    client, _ = settings_client()
    answer = client.put(
        "/api/settings/public-address",
        json={"public_base_url": "http://127.0.0.1:8420"},
        headers=OPERATOR,
    )
    assert answer.status_code == 200
    assert answer.json()["redirect_uri"] == "http://127.0.0.1:8420/oauth/callback"


@pytest.mark.parametrize(
    "address",
    [
        "http://arc.example.com",
        "https://arc.example.com/dashboard",
        "https://arc.example.com/?next=evil",
        "https://user:pw@arc.example.com",
        "ftp://arc.example.com",
        "not a url",
    ],
)
def test_bad_addresses_are_refused_and_audited(config: Path, address: str) -> None:
    client, audit = settings_client()
    answer = client.put(
        "/api/settings/public-address", json={"public_base_url": address}, headers=OPERATOR
    )
    assert answer.status_code == 400
    assert not (config / "gateway.toml").exists()
    assert audit.events[-1]["outcome"] == "denied"


def test_body_must_carry_the_field(config: Path) -> None:
    client, _ = settings_client()
    answer = client.put("/api/settings/public-address", json={"url": "x"}, headers=OPERATOR)
    assert answer.status_code == 400


# --- tailscale suggestion ---------------------------------------------------


def test_tailscale_serve_in_front_of_the_port_is_suggested(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arcui.routes import ui_settings

    status = {
        "Web": {"arc.tail1234.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8420"}}}}
    }

    async def fake_status() -> dict[str, object] | None:
        return status

    monkeypatch.setattr(ui_settings, "_tailscale_serve_status", fake_status)
    client, _ = settings_client()
    body = client.get("/api/settings/public-address", headers=VIEWER).json()
    assert body["suggestions"] == [{"source": "tailscale", "url": "https://arc.tail1234.ts.net"}]


def test_tailscale_serving_another_port_is_not_suggested(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arcui.routes import ui_settings

    async def fake_status() -> dict[str, object] | None:
        return {"Web": {"x.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}}}

    monkeypatch.setattr(ui_settings, "_tailscale_serve_status", fake_status)
    client, _ = settings_client()
    assert client.get("/api/settings/public-address", headers=VIEWER).json()["suggestions"] == []


# --- dashboard TLS ----------------------------------------------------------


def test_tls_starts_unconfigured(config: Path) -> None:
    client, _ = settings_client()
    body = client.get("/api/settings/tls", headers=VIEWER).json()
    assert body == {
        "configured": False,
        "active": False,
        "required": False,
        "subject": None,
        "not_after": None,
        "dns_names": [],
    }


def test_saved_certificate_is_served_on_the_next_start(config: Path) -> None:
    from arcui.ui_tls import serving_tls

    cert, key = self_signed()
    client, audit = settings_client()
    answer = client.put(
        "/api/settings/tls", json={"cert_pem": cert, "key_pem": key}, headers=OPERATOR
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["configured"] is True
    assert body["restart_required"] is True
    assert body["dns_names"] == ["arc.example.ts.net"]
    assert key not in answer.text
    assert audit.events[-1]["operation"] == "ui.tls.write"
    serving = serving_tls()
    assert serving is not None and serving.certfile.is_file()


def test_the_key_never_rests_on_disk_in_the_clear(config: Path) -> None:
    cert, key = self_signed()
    client, _ = settings_client()
    client.put("/api/settings/tls", json={"cert_pem": cert, "key_pem": key}, headers=OPERATOR)
    body = "".join(key.splitlines()[1:-1])
    for path in config.rglob("*"):
        if path.is_file():
            content = path.read_text(errors="ignore")
            assert body not in content, path
    stored_key = (config / "ui-tls.key.pem").read_text()
    assert "ENCRYPTED PRIVATE KEY" in stored_key
    assert (config / "ui-tls.key.pem").stat().st_mode & 0o077 == 0


def test_mismatched_key_is_refused(config: Path) -> None:
    cert, _ = self_signed()
    _, other_key = self_signed()
    client, audit = settings_client()
    answer = client.put(
        "/api/settings/tls", json={"cert_pem": cert, "key_pem": other_key}, headers=OPERATOR
    )
    assert answer.status_code == 400
    assert "does not belong" in answer.json()["error"]
    assert not (config / "ui-tls.cert.pem").exists()
    assert audit.events[-1]["outcome"] == "denied"


def test_expired_certificate_is_refused(config: Path) -> None:
    cert, key = self_signed(not_before=datetime.now(UTC) - timedelta(days=60), days=30)
    client, _ = settings_client()
    answer = client.put(
        "/api/settings/tls", json={"cert_pem": cert, "key_pem": key}, headers=OPERATOR
    )
    assert answer.status_code == 400


def test_garbage_pem_is_refused_without_echo(config: Path) -> None:
    client, _ = settings_client()
    answer = client.put(
        "/api/settings/tls",
        json={"cert_pem": "SENTINEL-NOT-A-CERT", "key_pem": "SENTINEL-NOT-A-KEY"},
        headers=OPERATOR,
    )
    assert answer.status_code == 400
    assert "SENTINEL" not in answer.text


def test_viewer_cannot_set_tls(config: Path) -> None:
    cert, key = self_signed()
    client, _ = settings_client()
    answer = client.put(
        "/api/settings/tls", json={"cert_pem": cert, "key_pem": key}, headers=VIEWER
    )
    assert answer.status_code == 403


def test_remove_forgets_the_certificate(config: Path) -> None:
    cert, key = self_signed()
    client, _ = settings_client(tls_active=True)
    client.put("/api/settings/tls", json={"cert_pem": cert, "key_pem": key}, headers=OPERATOR)
    answer = client.delete("/api/settings/tls", headers=OPERATOR)
    assert answer.status_code == 200
    assert answer.json()["configured"] is False
    assert answer.json()["active"] is True
    assert answer.json()["restart_required"] is True
    assert not (config / "ui-tls.key.pem").exists()


def test_federal_marks_tls_required_and_refuses_removal(config: Path) -> None:
    set_tier(config, fleet="federal")
    client, _ = settings_client()
    assert client.get("/api/settings/tls", headers=VIEWER).json()["required"] is True
    answer = client.delete("/api/settings/tls", headers=OPERATOR)
    assert answer.status_code == 409
