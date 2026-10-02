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
from packages.arcagent.tests.custody_fakes import make_cipher

from arcagent.connections import AuditChain, Connections
from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.connection_health import StoreHealthReporter
from arcagent.extension.credential_broker import credential_plan
from arcagent.extension.custody import CredentialRowStore
from arcagent.extension.custody_select import Custody, open_custody
from arcagent.extension.grants import ConnectionRegistry
from arcagent.extension.state import ConnectionStateStore, open_connection_state
from arcagent.modules.connectors.install import (
    ConnectorPlan,
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


def _custody(backend: FakeBackend) -> Custody:
    """Sealed custody over the test's operational plane — the same rows the façade uses."""

    async def opener() -> FakeBackend:
        return backend

    return open_custody(backend, make_cipher(), health=StoreHealthReporter(opener))


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
        credential_cipher=make_cipher(),
    )


async def _install_with_app_creds(tmp_path: Path, root: Path, backend: FakeBackend) -> None:
    """Install the connection holding only the app key/secret — no refresh token yet."""
    arc_dir = _arc_dir(tmp_path)
    custody = _custody(backend)
    plan = _plan(root)
    broker = custody.broker(registry=lambda: ConnectionRegistry(arc_dir))
    await install_connector(
        plan,
        connections=ConnectionRegistry(arc_dir),
        agents=[_AGENT],
        secret_values={"app_key": "ak-123", "app_secret": "as-456"},
        store=custody.store,
        caller_did=_CALLER,
        state=await _state(backend),
        credential=broker.operator_handle(
            _INSTANCE, actor_did=_CALLER, plan=credential_plan(plan.manifest)
        ),
    )


async def _refresh_token(backend: FakeBackend) -> str | None:
    """The refresh token as custody holds it: a sealed field, opened with the cipher."""
    rows = CredentialRowStore(backend, make_cipher())
    row = await rows.read(_INSTANCE)
    found = rows.open_field(row, "refresh_token") if row is not None else None
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
    monkeypatch.setattr("arcagent.connections.post_form", _ok_post)
    refreshed_with: list[str] = []

    async def _refresh_post(
        url: str, data: dict[str, str], auth: tuple[str, str]
    ) -> tuple[int, dict[str, Any]]:
        assert data["grant_type"] == "refresh_token"
        refreshed_with.append(data["refresh_token"])
        return 200, {"access_token": "at-renewed", "expires_in": 14400}

    # The rebuilt attachment reads a bearer through its handle, which renews from the
    # STORED refresh token: the provider endpoint is the one boundary faked here.
    monkeypatch.setattr("arcagent.extension.custody_select.post_form", _refresh_post)

    auth = await _connections(tmp_path, root, backend).complete_oauth(
        _INSTANCE, code="one-time-code"
    )

    assert await _refresh_token(backend) == "rt-durable-xyz", (
        "the durable refresh token the exchange returned must be persisted"
    )
    assert refreshed_with == [], (
        "the access token the exchange issued is stored with the refresh token and used; "
        "no refresh is spent right after connecting"
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
    monkeypatch.setattr("arcagent.connections.post_form", _dead_code_post)

    with pytest.raises(ExtensionError) as exc:
        await _connections(tmp_path, root, backend).complete_oauth(_INSTANCE, code="expired")

    assert "invalid_grant" in str(exc.value)
    assert await _refresh_token(backend) is None, "a failed exchange stores nothing"
