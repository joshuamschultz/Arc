"""P18-3.M — Microsoft 365 native OAuth abuse at the connector seam.

The real ``microsoft365`` bundle, the real ``Connections`` → custody → health path,
and only Microsoft's wire faked (:class:`FakeEntra`, :class:`FakeGraph`). The fake
token endpoint answers only on the bound tenant's authority, so every assertion
that "nothing reached Microsoft" is about the real URL Arc built.
"""

from __future__ import annotations

import logging
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from arcstore.backends.memory import FakeBackend
from arctrust.audit import AuditEvent
from packages.arcagent.tests.custody_fakes import make_cipher
from packages.arcagent.tests.microsoft_fakes import TENANT, FakeEntra, FakeGraph

from arcagent.connections import AuditChain, Connections
from arcagent.core.errors import ExtensionError
from arcagent.extension.connection_health import StoreHealthReporter
from arcagent.extension.credentials import CredentialRenewalError, RenewalPlanner
from arcagent.extension.custody import CredentialRowStore
from arcagent.extension.oauth import refresh_access_token
from arcagent.extension.oauth_apps import OAuthAppStore
from arcagent.extension.state import ConnectionStateStore

REPO = Path(__file__).resolve().parents[4]
UPN = "josh@agency.gov"
REDIRECT = "http://127.0.0.1:8420/oauth/callback"
INSTANCE = "work_mail"
OTHER_TENANT = "99999999-8888-7777-6666-555555555555"


class _Sink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class World:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.backend = FakeBackend()
        self.entra = FakeEntra()
        self.graph = FakeGraph(self.entra, upn=UPN)
        self.sink = _Sink()
        root = tmp_path / "extensions"
        shutil.copytree(
            REPO / "extensions" / "microsoft365",
            root / "microsoft365",
            ignore=shutil.ignore_patterns("__pycache__", "tests"),
        )
        (tmp_path / "arc").mkdir()
        monkeypatch.syspath_prepend(str(root / "microsoft365"))
        from arc_ext_microsoft365.native import graph as graph_module

        transport = self.graph.transport()
        monkeypatch.setattr(
            graph_module.GraphClient,
            "_new_client",
            lambda _self: httpx.AsyncClient(transport=transport),
        )

        async def opener() -> FakeBackend:
            return self.backend

        self.connections = Connections.for_deployment(
            arc_dir=tmp_path / "arc",
            data_dir=tmp_path / "data",
            extensions_root=root,
            audit=AuditChain.held(self.sink),
            state_opener=opener,
            credential_cipher=make_cipher(),
            oauth_redirect_uri=REDIRECT,
            token_post=self.entra.post,
        )

    async def setup(self) -> None:
        plan = self.connections.plan("microsoft365", INSTANCE)
        await self.connections.install(plan, {"account": UPN})
        await self.set_app(TENANT)

    async def set_app(self, tenant: str, cloud: str = "global") -> None:
        await self.connections.set_oauth_app(
            "microsoft",
            client_id=self.entra.client_id,
            client_secret=self.entra.client_secret,
            tenant_id=tenant,
            cloud=cloud,
        )

    async def connect(self, *, session: str = "op") -> Any:
        begun = await self.connections.begin_oauth(INSTANCE, session_id=session)
        landed = self.entra.consent(begun.authorize_url, email=UPN)
        return await self.connections.complete_oauth(session_id=session, redirect_url=landed)

    async def field(self, name: str) -> str | None:
        rows = CredentialRowStore(self.backend, make_cipher())
        row = await rows.read(INSTANCE)
        found = await rows.open_field(row, name) if row is not None else None
        return found.reveal() if found is not None else None

    def planner(self) -> RenewalPlanner:
        async def opener() -> FakeBackend:
            return self.backend

        async def no_sleep(_seconds: float) -> None:
            return None

        return RenewalPlanner(
            rows=CredentialRowStore(self.backend, make_cipher()),
            refresh=lambda request: refresh_access_token(request, post=self.entra.post),
            health=StoreHealthReporter(opener),
            owner_id="renewer",
            client=OAuthAppStore(self.backend, make_cipher()).client_for,
            state=ConnectionStateStore(self.backend),
            sleep=no_sleep,
        )


@pytest.fixture
async def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    ready = World(tmp_path, monkeypatch)
    await ready.setup()
    return ready


@pytest.fixture(autouse=True)
def _forget_bundle_modules() -> Iterator[None]:
    yield
    for name in [key for key in sys.modules if key.startswith("arc_ext_microsoft365")]:
        del sys.modules[name]


async def test_connect_binds_tenant_and_cloud_and_leaks_no_token(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    record = await world.connect()
    assert record.status == "healthy", record
    assert await world.field("tenant_id") == TENANT
    assert await world.field("cloud") == "global"
    assert await world.field("account") == UPN
    leaked = repr([event.model_dump() for event in world.sink.events]) + caplog.text
    for value in world.entra.secrets_seen():
        assert value not in leaked


async def test_forged_state_is_refused_and_nothing_reaches_entra(world: World) -> None:
    await world.connections.begin_oauth(INSTANCE, session_id="op")
    with pytest.raises(ExtensionError) as caught:
        await world.connections.complete_oauth(
            session_id="op", redirect_url=f"{REDIRECT}?state=forged&code=c"
        )
    assert caught.value.code == "OAUTH_STATE_INVALID"
    assert not world.entra.posts and await world.field("refresh_token") is None


async def test_pkce_mismatch_is_refused(world: World) -> None:
    """The code of one sign-in cannot be redeemed with another's verifier."""
    first = await world.connections.begin_oauth(INSTANCE, session_id="op")
    second = await world.connections.begin_oauth(INSTANCE, session_id="op")
    landed = world.entra.consent(first.authorize_url, email=UPN)
    swapped = landed.replace(f"state={first.state}", f"state={second.state}")
    with pytest.raises(ExtensionError):
        await world.connections.complete_oauth(session_id="op", redirect_url=swapped)
    assert await world.field("refresh_token") is None


@pytest.mark.parametrize(
    "claims",
    [
        {"tid": OTHER_TENANT},
        {"iss": f"https://login.microsoftonline.com/{OTHER_TENANT}/v2.0"},
        {"aud": "some-other-app"},
        {"preferred_username": "intruder@agency.gov"},
    ],
)
async def test_a_sign_in_from_another_tenant_app_or_user_is_refused(
    world: World, claims: dict[str, Any]
) -> None:
    world.entra.claims_override = claims
    with pytest.raises(ExtensionError) as caught:
        await world.connect()
    assert caught.value.code == "ACCOUNT_MISMATCH"
    assert await world.field("refresh_token") is None


async def test_slot_changed_between_begin_and_complete_is_refused(world: World) -> None:
    begun = await world.connections.begin_oauth(INSTANCE, session_id="op")
    landed = world.entra.consent(begun.authorize_url, email=UPN)
    await world.set_app(OTHER_TENANT)
    with pytest.raises(ExtensionError) as caught:
        await world.connections.complete_oauth(session_id="op", redirect_url=landed)
    assert caught.value.code == "OAUTH_STATE_INVALID"
    assert not world.entra.posts


async def test_a_tenant_a_refresh_token_is_never_sent_for_slot_b(world: World) -> None:
    await world.connect()
    posts_after_connect = len(world.entra.posts)
    await world.set_app(OTHER_TENANT)
    flow = world.connections.plan_for(INSTANCE).manifest.oauth
    assert flow is not None
    with pytest.raises(CredentialRenewalError) as caught:
        await world.planner().ensure_fresh(INSTANCE, flow=flow, force=True)
    assert caught.value.terminal
    assert len(world.entra.posts) == posts_after_connect
    records = await world.connections.health_records()
    assert records[INSTANCE].status == "needs_you"


@pytest.mark.parametrize(
    ("tenant", "cloud"),
    [
        ("common", "global"),
        ("organizations", "global"),
        ("agency.onmicrosoft.com", "global"),
        (TENANT, "evil.example"),
        (TENANT, "https://login.evil.example"),
        (TENANT, "china"),
        ("", "global"),
    ],
)
async def test_the_app_slot_cannot_point_at_an_arbitrary_host_or_tenant(
    world: World, tenant: str, cloud: str
) -> None:
    with pytest.raises(ExtensionError) as caught:
        await world.set_app(tenant, cloud)
    assert caught.value.code == "OAUTH_APP_INVALID"


async def test_gcc_high_cloud_is_a_config_choice(world: World) -> None:
    await world.set_app(TENANT, "usgov")
    begun = await world.connections.begin_oauth(INSTANCE, session_id="op")
    assert begun.authorize_url.startswith(
        f"https://login.microsoftonline.us/{TENANT}/oauth2/v2.0/authorize?"
    )


async def test_refresh_re_sends_scopes_and_persists_the_rotated_token(world: World) -> None:
    await world.connect()
    before = await world.field("refresh_token")
    flow = world.connections.plan_for(INSTANCE).manifest.oauth
    assert flow is not None
    assert await world.planner().ensure_fresh(INSTANCE, flow=flow, force=True)
    after = await world.field("refresh_token")
    assert after and after != before
    assert world.entra.refresh_scopes_seen and "Mail.Read" in world.entra.refresh_scopes_seen[0]
    assert world.entra.wrong_endpoint_posts == 0


async def test_revoked_refresh_token_is_needs_you_and_never_in_the_error(world: World) -> None:
    await world.connect()
    refresh = await world.field("refresh_token")
    world.entra.revoke_all()
    flow = world.connections.plan_for(INSTANCE).manifest.oauth
    assert flow is not None
    with pytest.raises(CredentialRenewalError) as caught:
        await world.planner().ensure_fresh(INSTANCE, flow=flow, force=True)
    assert refresh is not None and refresh not in str(caught.value)
    records = await world.connections.health_records()
    assert records[INSTANCE].status == "needs_you"
    assert refresh not in repr([event.model_dump() for event in world.sink.events])
