"""P18-3 §9 — native OAuth abuse at the connector seam (``Connections``).

The real ``Connections`` → custody → health path, a real bundle on disk, and a fake
provider token endpoint (:class:`FakeOAuthProvider`) that enforces single-use codes,
PKCE and redirect binding the way a real provider does.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend
from arctrust.audit import AuditEvent
from packages.arcagent.tests.custody_fakes import make_cipher
from packages.arcagent.tests.oauth_fakes import FakeOAuthProvider

from arcagent.connections import AuditChain, Connections
from arcagent.core.errors import ExtensionError
from arcagent.extension.custody import CredentialRowStore
from arcagent.extension.oauth_apps import OAUTH_APP_COLLECTION, OAuthAppStore

EMAIL = "josh@blackarcsystems.com"
REDIRECT = "http://127.0.0.1:8420/oauth/callback"
INSTANCE = "systems"

MANIFEST = """
[extension]
name = "gshape"
version = "1.0.0"
attachment = "native"

[config.native]
entrypoint = "gshape_attachment"

[[secrets]]
name = "account"
sensitive = false
required = false

[[secrets]]
name = "read_only"
sensitive = false
required = false
choices = ["yes", "no"]
default = "yes"

[[secrets]]
name = "refresh_token"
required = false

[oauth]
provider = "google"
authorize_url = "https://accounts.google.com/o/oauth2/v2/auth"
token_url = "https://oauth2.googleapis.com/token"
revoke_url = "https://oauth2.googleapis.com/revoke"
refresh_token_secret = "refresh_token"
client_auth = "post_form"
account = "openid_email"
id_token_issuers = ["https://accounts.google.com"]
scopes = ["openid", "email", "mail.write"]
scopes_read_only = ["openid", "email", "mail.read"]
authorize_params = { access_type = "offline", prompt = "consent" }

[health]
probe = "attachment"

[tools]
allow = ["ping"]

[[tools.declared]]
name = "ping"
description = "ping"
classification = "read_only"
"""

ADAPTER = """
from __future__ import annotations

from typing import Any

from arcagent.extension.attachment import ProbeResult, ToolResult, ToolSpec


class Shaped:
    def __init__(self, context: dict[str, Any]) -> None:
        self._credential = context["credential"]

    def requirements(self) -> list[Any]:
        return []

    async def probe(self) -> ProbeResult:
        token = (await self._credential.bearer()).reveal()
        return ProbeResult(reachable=token.startswith("ya29."), detail="probed")

    async def describe_tools(self) -> list[ToolSpec]:
        return [ToolSpec(name="ping", classification="read_only")]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(tool=tool, content="ok")


def build_native_attachment(context: dict[str, Any]) -> Shaped:
    return Shaped(context)
"""


class _Sink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class World:
    def __init__(self, tmp_path: Path, provider: FakeOAuthProvider) -> None:
        self.backend = FakeBackend()
        self.provider = provider
        self.sink = _Sink()
        root = tmp_path / "extensions"
        bundle = root / "gshape"
        bundle.mkdir(parents=True)
        (bundle / "extension.toml").write_text(MANIFEST, encoding="utf-8")
        (bundle / "gshape_attachment.py").write_text(ADAPTER, encoding="utf-8")
        (tmp_path / "arc").mkdir()

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
            token_post=provider.post,
        )

    async def setup(self, account: str = EMAIL, read_only: str = "yes") -> None:
        plan = self.connections.plan("gshape", INSTANCE)
        await self.connections.install(plan, {"account": account, "read_only": read_only})
        await self.connections.set_oauth_app(
            "google", client_id=self.provider.client_id, client_secret=self.provider.client_secret
        )

    async def connect(self, *, session: str = "op", email: str = EMAIL, **kw: Any) -> Any:
        begun = await self.connections.begin_oauth(INSTANCE, session_id=session)
        landed = self.provider.consent(begun.authorize_url, email=email, **kw)
        return await self.connections.complete_oauth(session_id=session, redirect_url=landed)

    async def field(self, name: str) -> str | None:
        rows = CredentialRowStore(self.backend, make_cipher())
        row = await rows.read(INSTANCE)
        found = await rows.open_field(row, name) if row is not None else None
        return found.reveal() if found is not None else None

    async def status(self) -> str:
        records = await self.connections.health_records()
        return records[INSTANCE].status


@pytest.fixture
async def world(tmp_path: Path) -> World:
    ready = World(tmp_path, FakeOAuthProvider())
    await ready.setup()
    return ready


async def test_happy_connect_is_healthy_and_leaks_nothing(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    record = await world.connect()
    assert record.status == "healthy"
    assert (await world.field("refresh_token") or "").startswith("1//rt-")
    rows = CredentialRowStore(world.backend, make_cipher())
    row = await rows.read(INSTANCE)
    assert row is not None and row.generation >= 1
    leaked = repr([event.model_dump() for event in world.sink.events]) + caplog.text
    for value in world.provider.secrets_seen():
        assert value not in leaked
    actions = {event.action for event in world.sink.events}
    assert {"connection.oauth.begin", "connection.oauth.complete"} <= actions


async def test_forged_state_is_refused(world: World) -> None:
    await world.connections.begin_oauth(INSTANCE, session_id="op")
    with pytest.raises(ExtensionError) as caught:
        await world.connections.complete_oauth(
            session_id="op", redirect_url=f"{REDIRECT}?state=forged&code=c"
        )
    assert caught.value.code == "OAUTH_STATE_INVALID"
    assert await world.field("refresh_token") is None and not world.provider.posts


async def test_session_swap_is_refused_without_burning_the_operator_flow(world: World) -> None:
    begun = await world.connections.begin_oauth(INSTANCE, session_id="op")
    landed = world.provider.consent(begun.authorize_url, email=EMAIL)
    with pytest.raises(ExtensionError) as caught:
        await world.connections.complete_oauth(session_id="intruder", redirect_url=landed)
    assert caught.value.code == "OAUTH_STATE_INVALID"
    assert not world.provider.posts
    record = await world.connections.complete_oauth(session_id="op", redirect_url=landed)
    assert record.status == "healthy"


async def test_code_replay_never_reaches_the_provider_twice(world: World) -> None:
    begun = await world.connections.begin_oauth(INSTANCE, session_id="op")
    landed = world.provider.consent(begun.authorize_url, email=EMAIL)
    await world.connections.complete_oauth(session_id="op", redirect_url=landed)
    with pytest.raises(ExtensionError):
        await world.connections.complete_oauth(session_id="op", redirect_url=landed)
    assert world.provider.exchanges == 1


async def test_pkce_verifier_is_bound_to_the_consent(world: World) -> None:
    """Two begun sign-ins: the code of one cannot be redeemed with the other's state."""
    first = await world.connections.begin_oauth(INSTANCE, session_id="op")
    second = await world.connections.begin_oauth(INSTANCE, session_id="op")
    landed = world.provider.consent(first.authorize_url, email=EMAIL)
    swapped = landed.replace(f"state={first.state}", f"state={second.state}")
    with pytest.raises(ExtensionError):
        await world.connections.complete_oauth(session_id="op", redirect_url=swapped)
    assert await world.field("refresh_token") is None


async def test_wrong_account_consent_is_refused_and_revoked(world: World) -> None:
    with pytest.raises(ExtensionError) as caught:
        await world.connect(email="hello@joshuaschultz.com")
    assert caught.value.code == "ACCOUNT_MISMATCH"
    assert await world.field("refresh_token") is None
    assert len(world.provider.revoked) == 1


async def test_scope_downgrade_records_needs_you(world: World) -> None:
    with pytest.raises(ExtensionError) as caught:
        await world.connect(granted_scope="openid email")
    assert caught.value.code == "SCOPE_MISSING"
    assert await world.field("refresh_token") is None
    records = await world.connections.health_records()
    assert records[INSTANCE].status == "needs_you"
    assert records[INSTANCE].reason_code == "scope_missing"


async def test_read_only_connection_asks_for_read_scopes_only(world: World) -> None:
    begun = await world.connections.begin_oauth(INSTANCE, session_id="op")
    assert "mail.read" in begun.authorize_url and "mail.write" not in begun.authorize_url


async def test_removal_revokes_the_refresh_token(world: World) -> None:
    await world.connect()
    refresh = await world.field("refresh_token")
    await world.connections.remove(INSTANCE)
    assert refresh in world.provider.revoked
    revoked = [e for e in world.sink.events if e.action == "connection.credential.revoked"]
    assert revoked and revoked[0].extra == {"ok": True}


async def test_app_slot_secret_is_sealed_and_bound_to_its_provider(world: World) -> None:
    raw = await world.backend.mutable_read(OAUTH_APP_COLLECTION, "google")
    assert raw is not None
    assert world.provider.client_secret not in repr(raw)
    apps = OAuthAppStore(world.backend, make_cipher())
    await apps.put("dropbox", client_id="dbx", client_secret="dbx-secret", actor_did="did:t")
    moved = dict(raw, provider="dropbox")
    await world.backend.mutable_merge(OAUTH_APP_COLLECTION, "dropbox", moved, actor_did="did:t")
    with pytest.raises(ExtensionError) as caught:
        await apps.get("dropbox")
    assert caught.value.code == "CREDENTIAL_UNREADABLE"


@pytest.mark.parametrize(
    ("client_id", "client_secret"),
    [("has space", "s"), ("ok", ""), ("ok", "line\nbreak"), ("ok", "x" * 600)],
)
async def test_app_slot_refuses_damaged_values(
    world: World, client_id: str, client_secret: str
) -> None:
    with pytest.raises(ExtensionError):
        await world.connections.set_oauth_app(
            "google", client_id=client_id, client_secret=client_secret
        )


async def test_a_redirect_changed_since_begin_is_refused(world: World, tmp_path: Path) -> None:
    """Config moved the redirect between begin and complete: the begun sign-in is void."""
    begun = await world.connections.begin_oauth(INSTANCE, session_id="op")
    landed = world.provider.consent(begun.authorize_url, email=EMAIL)
    code = landed.split("code=")[1].split("&")[0]
    world.connections._oauth_redirect_uri = "https://arc.example.com/oauth/callback"  # reason: simulates a config change in the same process
    with pytest.raises(ExtensionError) as caught:
        await world.connections.complete_oauth(session_id="op", state=begun.state, code=code)
    assert caught.value.code == "OAUTH_STATE_INVALID"
    assert not world.provider.posts


def _planner(world: World, owner: str) -> Any:
    from arcagent.extension.connection_health import StoreHealthReporter
    from arcagent.extension.credentials import RenewalPlanner
    from arcagent.extension.oauth import refresh_access_token
    from arcagent.extension.state import ConnectionStateStore

    async def opener() -> FakeBackend:
        return world.backend

    async def no_sleep(_seconds: float) -> None:
        return None

    return RenewalPlanner(
        rows=CredentialRowStore(world.backend, make_cipher()),
        refresh=lambda request: refresh_access_token(request, post=world.provider.post),
        health=StoreHealthReporter(opener),
        owner_id=owner,
        client=OAuthAppStore(world.backend, make_cipher()).client_for,
        state=ConnectionStateStore(world.backend),
        sleep=no_sleep,
    )


async def _flow(world: World) -> Any:
    return world.connections.plan_for(INSTANCE).manifest.oauth


async def test_refresh_keeps_a_non_rotating_token_and_persists_a_rotated_one(
    world: World,
) -> None:
    await world.connect()
    first = await world.field("refresh_token")
    assert await _planner(world, "p").ensure_fresh(INSTANCE, flow=await _flow(world), force=True)
    assert await world.field("refresh_token") == first, "Google does not rotate"

    world.provider.rotate_refresh = True
    assert await _planner(world, "p").ensure_fresh(INSTANCE, flow=await _flow(world), force=True)
    rotated = await world.field("refresh_token")
    assert rotated != first and first in world.provider.revoked
    assert await _planner(world, "q").ensure_fresh(INSTANCE, flow=await _flow(world), force=True)
    assert await world.field("refresh_token") not in (first, rotated)


async def test_invalid_grant_sets_needs_you_once_and_stops_calling(world: World) -> None:
    from arcagent.extension.credentials import CredentialRenewalError

    await world.connect()
    world.provider.revoke_all()
    with pytest.raises(CredentialRenewalError) as caught:
        await _planner(world, "p").ensure_fresh(INSTANCE, flow=await _flow(world), force=True)
    assert caught.value.error_code == "invalid_grant"
    records = await world.connections.health_records()
    assert records[INSTANCE].status == "needs_you"
    notices = records[INSTANCE].notice_seq
    posts = len(world.provider.posts)
    for owner in ("p", "q", "restarted"):
        with pytest.raises(CredentialRenewalError):
            await _planner(world, owner).ensure_fresh(
                INSTANCE, flow=await _flow(world), force=True
            )
    assert len(world.provider.posts) == posts, "a dead refresh token was presented again"
    records = await world.connections.health_records()
    assert records[INSTANCE].notice_seq == notices, "the operator was told more than once"
    record = await world.connect()
    assert record.status == "healthy", "reconnecting clears it"


class _VaultCipher:
    """A stand-in transit cipher: a different key and a different kind."""

    kind = "transit1"

    def __init__(self) -> None:
        self._inner = make_cipher("vault")

    def seal(self, plaintext: bytes, *, scope: str, slot: str) -> str:
        return self._inner.seal(plaintext, scope=scope, slot=slot)

    def open(self, sealed: str, *, scope: str, slot: str) -> bytes:
        return self._inner.open(sealed, scope=scope, slot=slot)


async def test_app_slot_reseals_under_the_vault_cipher(world: World) -> None:
    vault = OAuthAppStore(world.backend, _VaultCipher())
    with pytest.raises(ExtensionError) as caught:
        await vault.get("google")
    assert caught.value.code == "CREDENTIAL_UNREADABLE", "not readable before the move"
    assert await vault.reseal("google", source=make_cipher(), actor_did="did:t")
    app = await vault.get("google")
    assert app is not None and app.client_secret.reveal() == world.provider.client_secret
    assert await vault.sealed_by("google") == "transit1"
    assert not await vault.reseal("google", source=make_cipher(), actor_did="did:t")
    with pytest.raises(ExtensionError):
        await OAuthAppStore(world.backend, make_cipher()).get("google")
