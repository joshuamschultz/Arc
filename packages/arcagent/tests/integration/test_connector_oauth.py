"""The native OAuth connect flow, end to end against a real store.

``complete_oauth`` is the whole in-harness sign-in: read the operator-supplied
app key/secret, swap the one-time code for a DURABLE refresh token, store it, and
probe. These tests drive it the way a surface does — install a real connection,
supply only the app key/secret, then complete the code exchange — and assert the
refresh token was PERSISTED and delivered to the rebuilt attachment, not merely
returned. The only thing faked is the provider's HTTP endpoint (the one external
boundary), injected exactly as the exchange's own unit tests inject it.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend
from arctrust.audit import AuditEvent

from arcagent.connections import AuditChain, Connections
from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.grants import ConnectionRegistry
from arcagent.extension.secrets import LocalFileSecretBackend, SecretRef, SecretStore
from arcagent.extension.state import ConnectionStateStore, open_connection_state
from arcagent.modules.connectors.install import (
    ConnectorPlan,
    connector_env_file,
    install_connector,
    plan_connector,
)

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "oauth_extension"
_BUNDLE = "oauth_reference"
_INSTANCE = "primary"
_AGENT = "oauth_agent"
_CALLER = "did:arc:testorg:executor/oauth"


class _Sink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _bundle_root(tmp_path: Path) -> Path:
    root = tmp_path / "extensions"
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(_FIXTURE_DIR, root / _BUNDLE, ignore=shutil.ignore_patterns("__pycache__"))
    return root


def _arc_dir(tmp_path: Path) -> Path:
    root = tmp_path / "arc"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _store(arc_dir: Path) -> SecretStore:
    return SecretStore(LocalFileSecretBackend(connector_env_file(arc_dir)))


def _plan(root: Path) -> ConnectorPlan:
    return plan_connector(
        extensions_root=[root],
        extension=_BUNDLE,
        instance=_INSTANCE,
        tier=Tier.PERSONAL,
        audit_sink=_Sink(),
    )


async def _open_fake(backend: FakeBackend) -> FakeBackend:
    return backend


async def _state(backend: FakeBackend) -> ConnectionStateStore:
    return await open_connection_state(opener=lambda: _open_fake(backend))


def _connections(tmp_path: Path, root: Path, backend: FakeBackend) -> Connections:
    return Connections.for_deployment(
        arc_dir=_arc_dir(tmp_path),
        data_dir=tmp_path / "data",
        extensions_root=root,
        audit=AuditChain.held(_Sink()),
        state_opener=lambda: _open_fake(backend),
    )


async def _install_with_app_creds(tmp_path: Path, root: Path, backend: FakeBackend) -> None:
    """Install the connection holding only the app key/secret — no refresh token yet."""
    await install_connector(
        _plan(root),
        connections=ConnectionRegistry(_arc_dir(tmp_path)),
        agents=[_AGENT],
        secret_values={"app_key": "ak-123", "app_secret": "as-456"},
        store=_store(_arc_dir(tmp_path)),
        caller_did=_CALLER,
        state=await _state(backend),
    )


async def _refresh_token(arc_dir: Path) -> str | None:
    found = await _store(arc_dir).get(
        SecretRef(connection=_INSTANCE, field="refresh_token"), caller_did=_CALLER
    )
    return found.reveal() if found is not None else None


async def _ok_post(
    url: str, data: dict[str, str], auth: tuple[str, str]
) -> tuple[int, dict[str, Any]]:
    assert data["grant_type"] == "authorization_code"
    assert auth == ("ak-123", "as-456"), "the stored app key/secret authenticate the exchange"
    return 200, {"refresh_token": "rt-durable-xyz", "access_token": "at", "expires_in": 14400}


async def _dead_code_post(
    url: str, data: dict[str, str], auth: tuple[str, str]
) -> tuple[int, dict[str, Any]]:
    return 400, {
        "error": "invalid_grant",
        "error_description": "code doesn't exist or has expired",
    }


async def test_complete_oauth_stores_a_durable_refresh_token_and_connects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: a code becomes a stored refresh token, and the connection answers."""
    root = _bundle_root(tmp_path)
    backend = FakeBackend()
    await _install_with_app_creds(tmp_path, root, backend)
    monkeypatch.setattr("arcagent.connections._oauth_post", _ok_post)

    auth = await _connections(tmp_path, root, backend).complete_oauth(
        _INSTANCE, code="one-time-code"
    )

    assert await _refresh_token(_arc_dir(tmp_path)) == "rt-durable-xyz", (
        "the durable refresh token the exchange returned must be persisted"
    )
    assert "ak-123" in auth.authorize_url
    assert "token_access_type=offline" in auth.authorize_url
    # The rebuilt attachment was handed the stored refresh token, so it probes authenticated —
    # delivery, not just a return value (the producers-unwired lesson).
    assert auth.working
    assert "authenticated" in auth.detail


async def test_authorization_offers_the_url_and_never_asks_for_the_managed_token(
    tmp_path: Path,
) -> None:
    """An OAuth connector's operator supplies app key/secret and opens a URL — never a token."""
    root = _bundle_root(tmp_path)
    backend = FakeBackend()
    await _install_with_app_creds(tmp_path, root, backend)

    auth = await _connections(tmp_path, root, backend).authorization(_INSTANCE)

    assert auth.oauth is True
    assert auth.oauth_connect is True
    assert "ak-123" in auth.authorize_url
    supplied = {credential.name for credential in auth.credentials}
    assert "refresh_token" not in supplied, "the managed token is never an operator field"
    assert {"app_key", "app_secret"} <= supplied


async def test_a_dead_code_refuses_and_stores_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """invalid_grant is terminal: the operator re-authorizes, and no bad token is persisted."""
    root = _bundle_root(tmp_path)
    backend = FakeBackend()
    await _install_with_app_creds(tmp_path, root, backend)
    monkeypatch.setattr("arcagent.connections._oauth_post", _dead_code_post)

    with pytest.raises(ExtensionError) as exc:
        await _connections(tmp_path, root, backend).complete_oauth(_INSTANCE, code="expired")

    assert "invalid_grant" in str(exc.value)
    assert await _refresh_token(_arc_dir(tmp_path)) is None, "a failed exchange stores nothing"
