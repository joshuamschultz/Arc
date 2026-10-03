"""The native OAuth connect flow, end to end against a real store.

One click is ``begin_oauth`` (a consent URL bound to the operator's session) then
``complete_oauth`` (swap the code for a DURABLE refresh token, seal it, probe). These
tests drive both the way a surface does — set the provider's app up once, install a
connection that holds no credential, consent at the fake provider, hand the address
the browser landed on back — and assert the refresh token was PERSISTED and delivered
to the rebuilt attachment, not merely returned. Only the provider's HTTP is faked.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from arcstore.backends.memory import FakeBackend
from arctrust.audit import AuditEvent
from packages.arcagent.tests.custody_fakes import make_cipher
from packages.arcagent.tests.oauth_fakes import FakeOAuthProvider

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
_REDIRECT = "http://127.0.0.1:8420/oauth/callback"
_SESSION = "session-A"


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


def _connections(
    tmp_path: Path, root: Path, backend: FakeBackend, provider: FakeOAuthProvider
) -> Connections:
    return Connections.for_deployment(
        arc_dir=_arc_dir(tmp_path),
        data_dir=tmp_path / "data",
        extensions_root=root,
        audit=AuditChain.held(_Sink()),
        state_opener=lambda: _open_fake(backend),
        credential_cipher=make_cipher(),
        oauth_redirect_uri=_REDIRECT,
        token_post=provider.post,
    )


async def _install(tmp_path: Path, root: Path, backend: FakeBackend) -> None:
    """Install the connection holding no credential yet: the refresh token is Arc's to write."""
    arc_dir = _arc_dir(tmp_path)
    custody = _custody(backend)
    plan = _plan(root)
    broker = custody.broker(registry=lambda: ConnectionRegistry(arc_dir))
    await install_connector(
        plan,
        connections=ConnectionRegistry(arc_dir),
        agents=[_AGENT],
        secret_values={},
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
    found = await rows.open_field(row, "refresh_token") if row is not None else None
    return found.reveal() if found is not None else None


async def _ready(tmp_path: Path) -> tuple[Connections, FakeOAuthProvider, FakeBackend]:
    """An installed connection and its provider's app set up once for the deployment."""
    root = _bundle_root(tmp_path)
    backend = FakeBackend()
    provider = FakeOAuthProvider()
    await _install(tmp_path, root, backend)
    connections = _connections(tmp_path, root, backend, provider)
    await connections.set_oauth_app(
        _BUNDLE, client_id=provider.client_id, client_secret=provider.client_secret
    )
    return connections, provider, backend


async def test_one_click_stores_a_durable_refresh_token_and_connects(tmp_path: Path) -> None:
    """The whole point: a consent becomes a stored refresh token, and the connection answers."""
    connections, provider, backend = await _ready(tmp_path)

    begun = await connections.begin_oauth(_INSTANCE, session_id=_SESSION)
    assert begun.redirect_mode == "callback"
    landed = provider.consent(begun.authorize_url, email="anyone@example.com")
    record = await connections.complete_oauth(session_id=_SESSION, redirect_url=landed)

    assert record.connection == _INSTANCE
    stored = await _refresh_token(backend)
    assert stored is not None and stored in provider.secrets_seen(), (
        "the durable refresh token the exchange returned must be persisted"
    )
    assert provider.exchanges == 1
    # The rebuilt attachment bears the stored credential, so it probes authenticated:
    # delivery, not just a return value (the producers-unwired lesson).
    auth = await connections.authorization(_INSTANCE)
    assert auth.working
    assert "authenticated" in auth.detail


async def test_authorization_marks_an_oauth_connector_and_never_asks_for_a_credential(
    tmp_path: Path,
) -> None:
    """An OAuth connector's operator clicks Connect — there is no token or app key to type."""
    connections, _provider, _backend = await _ready(tmp_path)

    auth = await connections.authorization(_INSTANCE)

    assert auth.oauth is True
    assert "refresh_token" not in {credential.name for credential in auth.credentials}


async def test_begin_refuses_until_the_provider_app_is_set_up(tmp_path: Path) -> None:
    """No app slot: the refusal names the redirect address to register, and starts nothing."""
    root = _bundle_root(tmp_path)
    backend = FakeBackend()
    provider = FakeOAuthProvider()
    await _install(tmp_path, root, backend)
    connections = _connections(tmp_path, root, backend, provider)

    with pytest.raises(ExtensionError) as exc:
        await connections.begin_oauth(_INSTANCE, session_id=_SESSION)

    assert exc.value.code == "OAUTH_APP_MISSING"
    assert _REDIRECT in str(exc.value)


async def test_a_dead_code_refuses_and_stores_nothing(tmp_path: Path) -> None:
    """invalid_grant is terminal: the operator reconnects, and no bad token is persisted."""
    connections, provider, backend = await _ready(tmp_path)
    begun = await connections.begin_oauth(_INSTANCE, session_id=_SESSION)
    landed = provider.consent(begun.authorize_url, email="anyone@example.com")
    spent_code = parse_qs(urlsplit(landed).query)["code"][0]
    provider._codes.pop(spent_code)  # the provider no longer knows this code

    with pytest.raises(ExtensionError) as exc:
        await connections.complete_oauth(session_id=_SESSION, redirect_url=landed)

    assert exc.value.code == "OAUTH_EXCHANGE_FAILED"
    assert await _refresh_token(backend) is None, "a failed exchange stores nothing"


async def test_a_sign_in_cannot_be_completed_from_another_session(tmp_path: Path) -> None:
    """The pending sign-in is bound to the session that began it; nothing is stored otherwise."""
    connections, provider, backend = await _ready(tmp_path)
    begun = await connections.begin_oauth(_INSTANCE, session_id=_SESSION)
    landed = provider.consent(begun.authorize_url, email="anyone@example.com")

    with pytest.raises(ExtensionError) as exc:
        await connections.complete_oauth(session_id="someone-else", redirect_url=landed)

    assert exc.value.code == "OAUTH_STATE_INVALID"
    assert await _refresh_token(backend) is None
