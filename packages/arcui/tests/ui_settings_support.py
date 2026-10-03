"""Shared harness for the public-address and dashboard-TLS settings tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware
from arcui.public_address import PublicAddress
from arcui.routes.ui_settings import routes as ui_settings_routes

OPERATOR = {"Authorization": "Bearer operator"}
VIEWER = {"Authorization": "Bearer viewer"}


class RecordingAudit:
    """Stands in for ``app.state.audit``: keeps every UI mutation event."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def audit_event(self, _kind: Any, fields: dict[str, Any]) -> None:
        self.events.append(fields)


def isolate(tmp_path: Path, monkeypatch: Any) -> Path:
    """Point every Arc path at ``tmp_path``; return the config dir."""
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path))
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    config = tmp_path / "config"
    config.mkdir(parents=True, exist_ok=True)
    return config


def set_tier(config: Path, *, fleet: str | None = None, gateway: str | None = None) -> None:
    """Write the tier floor (arcagent.toml) and/or the gateway tier (gateway.toml)."""
    if fleet is not None:
        (config / "arcagent.toml").write_text(f'[security]\ntier = "{fleet}"\n')
    if gateway is not None:
        (config / "gateway.toml").write_text(f'[gateway]\ntier = "{gateway}"\n')


def settings_client(*, tls_active: bool = False) -> tuple[TestClient, RecordingAudit]:
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=ui_settings_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.public_address = PublicAddress(ui_port=8420)
    app.state.ui_tls_active = tls_active
    audit = RecordingAudit()
    app.state.audit = audit
    return TestClient(app), audit


def self_signed(
    host: str = "arc.example.ts.net",
    *,
    not_before: datetime | None = None,
    days: int = 30,
) -> tuple[str, str]:
    """A fresh EC key and a self-signed certificate for ``host`` (PEM strings)."""
    key = ec.generate_private_key(ec.SECP256R1())
    start = not_before or datetime.now(UTC) - timedelta(minutes=5)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return cert.public_bytes(serialization.Encoding.PEM).decode(), key_pem
