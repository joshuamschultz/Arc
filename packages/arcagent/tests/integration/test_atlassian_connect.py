"""Atlassian 3LO connect, rotation and site binding, end to end (P18-3, 3.T).

The SHIPPED ``jira`` bundle (native REST attachment), the real ``Connections``
begin/complete path, real sealed custody and the real renewer. Only Atlassian's
HTTP is fake (``packages/arcagent/tests/atlassian_fakes.py``): it enforces
rotating single-use refresh tokens, JSON token bodies, no PKCE and the list of
sites a token reaches.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from arcstore.backends.memory import FakeBackend
from arctrust.audit import AuditEvent
from packages.arcagent.tests.atlassian_fakes import SITE_A, SITE_B, FakeAtlassian, FakeJira
from packages.arcagent.tests.custody_fakes import (
    InterleavingBackend,
    make_cipher,
    once_per_task,
)

from arcagent.connections import AuditChain, Connections
from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.connection_health import StoreHealthReporter
from arcagent.extension.credential_broker import credential_plan
from arcagent.extension.credentials import CredentialRenewalError, RefreshRequest, RenewalPlanner
from arcagent.extension.custody import CREDENTIAL_COLLECTION, CredentialRowStore
from arcagent.extension.custody_select import Custody, open_custody
from arcagent.extension.grants import ConnectionRegistry
from arcagent.extension.manifest import load_manifest
from arcagent.extension.oauth import refresh_access_token
from arcagent.extension.oauth_apps import OAuthApp
from arcagent.extension.secrets import Secret
from arcagent.extension.state import ConnectionStateStore, open_connection_state
from arcagent.modules.connectors.install import install_connector, plan_connector

_REPO = Path(__file__).resolve().parents[4]
_BUNDLE = "jira"
_INSTANCE = "acme"
_AGENT = "reader"
_CALLER = "did:arc:testorg:executor/atlassian"
_REDIRECT = "http://127.0.0.1:8420/oauth/callback"
_SESSION = "session-A"
_ACTOR = "did:arc:operator:test"


class _Sink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


@pytest.fixture
def bundle_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "extensions"
    root.mkdir()
    shutil.copytree(
        _REPO / "extensions" / _BUNDLE,
        root / _BUNDLE,
        ignore=shutil.ignore_patterns("__pycache__", "tests"),
    )
    monkeypatch.syspath_prepend(str(root / _BUNDLE))
    yield root
    for name in [key for key in sys.modules if key.startswith("arc_ext_jira")]:
        del sys.modules[name]


def _route_jira(monkeypatch: pytest.MonkeyPatch, jira: FakeJira) -> None:
    from arc_ext_jira.native import JiraHttp

    transport = jira.transport()
    monkeypatch.setattr(
        JiraHttp, "_new_client", lambda self: httpx.AsyncClient(transport=transport)
    )


async def _opener(backend: FakeBackend) -> FakeBackend:
    return backend


def _custody(backend: FakeBackend) -> Custody:
    return open_custody(
        backend, make_cipher(), health=StoreHealthReporter(lambda: _opener(backend))
    )


class _World:
    def __init__(self, tmp_path: Path, root: Path, backend: FakeBackend) -> None:
        self.tmp_path = tmp_path
        self.root = root
        self.backend = backend
        self.provider = FakeAtlassian()
        self.arc_dir = tmp_path / "arc"
        self.arc_dir.mkdir(exist_ok=True)

    def connections(self) -> Connections:
        return Connections.for_deployment(
            arc_dir=self.arc_dir,
            data_dir=self.tmp_path / "data",
            extensions_root=self.root,
            audit=AuditChain.held(_Sink()),
            state_opener=lambda: _opener(self.backend),
            credential_cipher=make_cipher(),
            oauth_redirect_uri=_REDIRECT,
            token_post=self.provider.post,
        )

    async def install(self, *, site: str = "") -> None:
        custody = _custody(self.backend)
        plan = plan_connector(
            extensions_root=[self.root],
            extension=_BUNDLE,
            instance=_INSTANCE,
            tier=Tier.PERSONAL,
            audit_sink=_Sink(),
        )
        broker = custody.broker(registry=lambda: ConnectionRegistry(self.arc_dir))
        await install_connector(
            plan,
            connections=ConnectionRegistry(self.arc_dir),
            agents=[_AGENT],
            secret_values={"site": site} if site else {},
            store=custody.store,
            caller_did=_CALLER,
            state=await open_connection_state(opener=lambda: _opener(self.backend)),
            credential=broker.operator_handle(
                _INSTANCE, actor_did=_CALLER, plan=credential_plan(plan.manifest)
            ),
        )

    async def ready(self, *, site: str = "acme.atlassian.net") -> Connections:
        await self.install(site=site)
        connections = self.connections()
        await connections.set_oauth_app(
            "atlassian",
            client_id=self.provider.client_id,
            client_secret=self.provider.client_secret,
        )
        return connections

    async def connect(self, connections: Connections) -> None:
        begun = await connections.begin_oauth(_INSTANCE, session_id=_SESSION)
        landed = self.provider.consent(begun.authorize_url, email="ann@acme.example")
        await connections.complete_oauth(session_id=_SESSION, redirect_url=landed)

    def rows(self) -> CredentialRowStore:
        return CredentialRowStore(self.backend, make_cipher())

    async def stored(self, name: str) -> str | None:
        row = await self.rows().read(_INSTANCE)
        found = await self.rows().open_field(row, name) if row is not None else None
        return found.reveal() if found is not None else None


@pytest.fixture
def world(tmp_path: Path, bundle_root: Path) -> _World:
    return _World(tmp_path, bundle_root, FakeBackend())


async def test_one_click_connect_binds_the_site_and_the_tools_bear_the_access_token(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    jira = FakeJira(world.provider)
    _route_jira(monkeypatch, jira)
    connections = await world.ready()

    begun = await connections.begin_oauth(_INSTANCE, session_id=_SESSION)
    query = {k: v[0] for k, v in parse_qs(urlsplit(begun.authorize_url).query).items()}
    assert query["audience"] == "api.atlassian.com"
    assert "code_challenge" not in query, "Atlassian 3LO is not a PKCE flow"
    assert "offline_access" in query["scope"].split()
    landed = world.provider.consent(begun.authorize_url, email="ann@acme.example")
    record = await connections.complete_oauth(session_id=_SESSION, redirect_url=landed)

    assert record.connection == _INSTANCE
    assert await world.stored("cloud_id") == SITE_A[0]
    assert await world.stored("site") == "acme.atlassian.net"
    refresh = await world.stored("refresh_token")
    assert refresh is not None and refresh in world.provider.secrets_seen()
    auth = await connections.authorization(_INSTANCE)
    assert auth.working, "the probe (GET myself) answered with the stored access token"
    assert jira.rejected == 0
    assert all(bearer in world.provider.issued_access for bearer in jira.bearers())


async def test_blank_site_takes_the_only_site_and_two_sites_require_a_choice(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _route_jira(monkeypatch, FakeJira(world.provider))
    world.provider.sites = [SITE_A, SITE_B]
    connections = await world.ready(site="")

    with pytest.raises(ExtensionError) as exc:
        await world.connect(connections)

    assert exc.value.code == "ACCOUNT_MISMATCH"
    assert "acme.atlassian.net" in str(exc.value) and "other.atlassian.net" in str(exc.value)
    assert SITE_A[0] not in str(exc.value), "the refusal names hosts, never ids"
    assert await world.stored("refresh_token") is None, "a refused connect stores nothing"


async def test_signing_in_to_a_site_the_connection_is_not_for_stores_nothing(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _route_jira(monkeypatch, FakeJira(world.provider))
    world.provider.sites = [SITE_B]  # the account reaches only the OTHER site
    connections = await world.ready(site="acme.atlassian.net")

    with pytest.raises(ExtensionError) as exc:
        await world.connect(connections)

    assert exc.value.code == "ACCOUNT_MISMATCH"
    assert await world.stored("cloud_id") is None
    assert await world.stored("refresh_token") is None


async def test_cloud_id_mismatch_on_reconnect_cannot_rebind_a_connection(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _route_jira(monkeypatch, FakeJira(world.provider))
    connections = await world.ready()
    await world.connect(connections)
    first_refresh = await world.stored("refresh_token")
    # The same host now resolves to a different Atlassian site id.
    world.provider.sites = [("cloud-cccc-3333", "https://acme.atlassian.net")]

    with pytest.raises(ExtensionError) as exc:
        await world.connect(connections)

    assert exc.value.code == "ACCOUNT_MISMATCH"
    assert await world.stored("cloud_id") == SITE_A[0]
    assert await world.stored("refresh_token") == first_refresh


# --- rotation ---------------------------------------------------------------------


def _planner(world: _World, owner: str, backend: FakeBackend, now: datetime) -> RenewalPlanner:
    async def refresh(request: RefreshRequest):  # type: ignore[no-untyped-def]  # test shim
        return await refresh_access_token(request, post=world.provider.post)

    async def client(_flow: object) -> OAuthApp:
        return OAuthApp(
            provider="atlassian",
            client_id=world.provider.client_id,
            client_secret=Secret(world.provider.client_secret),
        )

    async def open_backend() -> FakeBackend:
        return backend

    async def instant(_seconds: float) -> None:
        await asyncio.sleep(0)

    return RenewalPlanner(
        rows=CredentialRowStore(backend, make_cipher(), clock=lambda: now),
        refresh=refresh,
        health=StoreHealthReporter(open_backend),
        owner_id=owner,
        client=client,
        state=ConnectionStateStore(backend),
        sink=_Sink(),
        clock=lambda: now,
        sleep=instant,
    )


def _flow(world: _World):  # type: ignore[no-untyped-def]  # test helper
    manifest = load_manifest(
        (world.root / _BUNDLE / "extension.toml").read_text(encoding="utf-8"), tier=Tier.PERSONAL
    )
    assert manifest.oauth is not None
    return manifest.oauth


async def test_two_processes_racing_a_rotating_refresh_spend_the_token_once(
    tmp_path: Path, bundle_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = InterleavingBackend()
    world = _World(tmp_path, bundle_root, backend)
    _route_jira(monkeypatch, FakeJira(world.provider))
    connections = await world.ready()
    await world.connect(connections)
    original = await world.stored("refresh_token")
    due = datetime.now(UTC) + timedelta(minutes=50)  # past 75 percent of the hour
    first = _planner(world, "proc-a", backend, due)
    second = _planner(world, "proc-b", backend, due)
    barrier = asyncio.Barrier(2)
    backend.before_update_if = once_per_task(barrier.wait, collection=CREDENTIAL_COLLECTION)

    flow = _flow(world)
    results = await asyncio.gather(
        first.ensure_fresh(_INSTANCE, flow=flow), second.ensure_fresh(_INSTANCE, flow=flow)
    )

    assert sorted(results) == [False, True]
    assert world.provider.refreshes == 1, "exactly one process spent the rotating token"
    rotated = await world.stored("refresh_token")
    assert rotated != original and rotated in world.provider.secrets_seen()
    assert original in world.provider.revoked


async def test_reuse_of_an_old_refresh_token_is_refused(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _route_jira(monkeypatch, FakeJira(world.provider))
    connections = await world.ready()
    await world.connect(connections)
    old = await world.stored("refresh_token")
    assert old is not None
    due = datetime.now(UTC) + timedelta(minutes=50)
    renewed = await _planner(world, "proc-a", world.backend, due).ensure_fresh(
        _INSTANCE, flow=_flow(world)
    )
    assert renewed is True

    with pytest.raises(CredentialRenewalError) as exc:
        await refresh_access_token(
            RefreshRequest(
                flow=_flow(world),
                refresh_token=Secret(old),
                client_id=world.provider.client_id,
                client_secret=Secret(world.provider.client_secret),
            ),
            post=world.provider.post,
        )

    assert exc.value.error_code == "invalid_grant"
    assert await world.stored("refresh_token") != old, "custody holds the rotated token"
