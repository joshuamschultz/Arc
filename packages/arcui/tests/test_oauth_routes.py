"""P18-3 §1.4 — one-click OAuth connect through the real arcui routes.

Only the provider's HTTP is fake (:class:`FakeOAuthProvider`); the routes, the
connector seam, custody, the health check and the bundle are real.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from packages.arcagent.tests.oauth_fakes import FakeOAuthProvider
from packages.arcui.tests.credential_custody import raw_custody_rows
from packages.arcui.tests.test_connectors_routes import (
    _AGENT,
    _agent,
    _bundles,
    _custody_value,
    _headers,
    world,
)
from starlette.requests import Request

__all__ = ["world"]

EMAIL = "josh@blackarcindustrial.com"
REDIRECT = "http://127.0.0.1:8420/oauth/callback"

_MANIFEST = """
[extension]
name = "acme_google"
version = "1.0.0"
attachment = "native"
description = "A Google-shaped native OAuth connector."

[config.native]
entrypoint = "acme_google_attachment"

[[secrets]]
name = "account"
sensitive = false
required = false

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
scopes = ["openid", "email", "https://www.googleapis.com/auth/gmail.readonly"]
authorize_params = { access_type = "offline", prompt = "consent" }

[health]
probe = "attachment"

[tools]
allow = ["ping"]

[[tools.declared]]
name = "ping"
description = "ping"
classification = "read_only"

[approval]
default = "outbound"
"""

_ADAPTER = """
from __future__ import annotations

from typing import Any

from arcagent.extension.attachment import ProbeResult, ToolResult, ToolSpec


class GoogleShaped:
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


def build_native_attachment(context: dict[str, Any]) -> GoogleShaped:
    return GoogleShaped(context)
"""


class OAuthWorld:
    """An arcui app with the connector routes, a fake Google, and two operator sessions."""

    def __init__(self, world: Path) -> None:
        self.client, _agent_id, _dir = _agent(world)
        self.world = world
        self.provider = FakeOAuthProvider()
        self.client.app.state.oauth_token_post = self.provider.post
        self.client.app.state.oauth_redirect_uri = REDIRECT
        self.client.app.add_middleware(_SessionFromHeader)
        bundle = _bundles(world) / "acme_google"
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "extension.toml").write_text(_MANIFEST, encoding="utf-8")
        (bundle / "acme_google_attachment.py").write_text(_ADAPTER, encoding="utf-8")

    def headers(self, session: str = "victim", token: str = "operator") -> dict[str, str]:
        return {**_headers(token), "x-test-session": session}

    def install(self, instance: str = "blackarc", account: str = EMAIL) -> None:
        answer = self.client.post(
            "/api/connections",
            json={
                "extension": "acme_google",
                "instance": instance,
                "agents": [_AGENT],
                "secrets": {"account": account},
            },
            headers=self.headers(),
        )
        assert answer.status_code == 200, answer.text

    def set_app(self) -> None:
        answer = self.client.put(
            "/api/oauth-apps/google",
            json={
                "client_id": self.provider.client_id,
                "client_secret": self.provider.client_secret,
            },
            headers=self.headers(),
        )
        assert answer.status_code == 200, answer.text

    def begin(
        self,
        instance: str = "blackarc",
        session: str = "victim",
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        return self.client.post(
            f"/api/connections/{instance}/oauth/begin",
            json={},
            headers={**self.headers(session), **(extra_headers or {})},
        )

    def complete(self, body: dict[str, str], session: str = "victim") -> Any:
        return self.client.post("/api/oauth/complete", json=body, headers=self.headers(session))

    def custody(self, field: str, instance: str = "blackarc") -> str | None:
        return _custody_value(self.client, self.world, instance, field)

    def raw_rows(self) -> str:
        return raw_custody_rows(self.client.app.state.arcstore_backend)

    def raw_rows_all(self) -> str:
        """Every stored row of every collection, as stored (ciphertext included)."""
        return repr(self.client.app.state.arcstore_backend.__dict__)


class _SessionFromHeader:
    """Test-only stand-in for the auth layer's per-session id (two browsers, one token)."""

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            session = Request(scope).headers.get("x-test-session")
            if session:
                scope.setdefault("state", {})["session_id"] = session
        await self.app(scope, receive, send)


@pytest.fixture
def oauth(world: Path) -> OAuthWorld:
    return OAuthWorld(world)


def connected(oauth: OAuthWorld, *, email: str = EMAIL) -> dict[str, Any]:
    """Run the whole one-click connect; returns the row the complete answered."""
    oauth.install()
    oauth.set_app()
    begun = oauth.begin()
    assert begun.status_code == 200, begun.text
    landed = oauth.provider.consent(begun.json()["authorize_url"], email=email)
    done = oauth.complete({"redirect_url": landed})
    assert done.status_code == 200, done.text
    return dict(done.json())


def test_one_click_connect_seals_the_grant_and_turns_healthy(oauth: OAuthWorld) -> None:
    row = connected(oauth)
    assert row["instance"] == "blackarc"
    assert row["status"] == "healthy", row
    assert row["connect_kind"] == "oauth" and row["oauth_provider"] == "google"
    refresh = oauth.custody("refresh_token")
    assert refresh is not None and refresh.startswith("1//rt-")
    assert refresh not in oauth.raw_rows(), "custody holds ciphertext only"
    for value in oauth.provider.secrets_seen():
        assert value not in str(row)
    assert oauth.provider.exchanges == 1


def test_begin_answers_a_pkce_url_with_the_configured_redirect(oauth: OAuthWorld) -> None:
    oauth.install()
    oauth.set_app()
    body = oauth.begin().json()
    assert body["redirect_mode"] == "callback" and body["expires_in"] == 600
    url = body["authorize_url"]
    assert "code_challenge_method=S256" in url
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8420%2Foauth%2Fcallback" in url
    assert f"state={body['state']}" in url
    assert "login_hint=josh%40blackarcindustrial.com" in url


def test_blank_account_is_filled_from_the_verified_sign_in(oauth: OAuthWorld) -> None:
    oauth.install(account="")
    oauth.set_app()
    begun = oauth.begin().json()
    landed = oauth.provider.consent(begun["authorize_url"], email="New@Example.com")
    assert oauth.complete({"redirect_url": landed}).status_code == 200
    assert oauth.custody("account") == "new@example.com"


def test_app_slot_is_set_once_and_never_returns_the_secret(oauth: OAuthWorld) -> None:
    before = oauth.client.get("/api/oauth-apps/google", headers=oauth.headers(token="viewer"))
    assert before.status_code == 200
    assert before.json()["configured"] is False
    assert before.json()["redirect_uri"] == REDIRECT
    oauth.set_app()
    after = oauth.client.get("/api/oauth-apps/google", headers=oauth.headers(token="viewer"))
    body = after.json()
    assert body["configured"] is True
    assert body["client_id_hint"] == oauth.provider.client_id[:12]
    assert oauth.provider.client_secret not in after.text
    assert oauth.provider.client_secret not in oauth.raw_rows_all()


def test_begin_without_an_app_names_the_redirect_uri_to_register(oauth: OAuthWorld) -> None:
    oauth.install()
    refused = oauth.begin()
    assert refused.status_code == 400
    assert REDIRECT in refused.json()["error"]


def test_paste_fallback_completes_from_the_pasted_address(oauth: OAuthWorld) -> None:
    """The callback tab had no session: the operator pastes the address into the Arc tab."""
    oauth.install()
    oauth.set_app()
    begun = oauth.begin().json()
    landed = oauth.provider.consent(begun["authorize_url"], email=EMAIL)
    pasted = f"  {landed}\n"
    assert oauth.complete({"redirect_url": pasted}).status_code == 200


def test_decline_stores_nothing(oauth: OAuthWorld) -> None:
    oauth.install()
    oauth.set_app()
    begun = oauth.begin().json()
    refused = oauth.complete({"redirect_url": oauth.provider.decline(begun["authorize_url"])})
    assert refused.status_code == 400
    assert refused.json()["error"].startswith("You declined access")
    assert oauth.custody("refresh_token") is None
