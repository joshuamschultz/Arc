"""P18-3 §9 — native OAuth abuse through the real routes.

State forgery, login swap, code replay, redirect tampering by Host header, a
wrong-account consent, a scope downgrade, role checks, secret disclosure and
provider-authored HTML. Only the provider's HTTP is fake.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from packages.arcui.tests.test_oauth_routes import EMAIL, REDIRECT, OAuthWorld, world

__all__ = ["world"]


@pytest.fixture
def oauth(world: Path) -> OAuthWorld:
    ready = OAuthWorld(world)
    ready.install()
    ready.set_app()
    return ready


def _state(url: str) -> str:
    return parse_qs(urlsplit(url).query)["state"][0]


def test_forged_state_is_refused(oauth: OAuthWorld) -> None:
    oauth.begin()
    forged = f"{REDIRECT}?state=forged-state-value&code=4/0anything"
    refused = oauth.complete({"redirect_url": forged})
    assert refused.status_code == 400
    assert oauth.custody("refresh_token") is None
    assert oauth.provider.exchanges == 0 and not oauth.provider.posts


def test_login_swap_attacker_code_into_victim_session_is_refused(oauth: OAuthWorld) -> None:
    """The attacker begins on THEIR session and lures the victim to their callback URL."""
    attacker = oauth.begin(session="attacker").json()
    lure = oauth.provider.consent(attacker["authorize_url"], email="attacker@evil.example")
    refused = oauth.complete({"redirect_url": lure}, session="victim")
    assert refused.status_code == 400
    assert oauth.custody("refresh_token") is None
    assert not oauth.provider.posts, "the attacker's code never reached the token endpoint"


def test_code_replay_is_refused_by_ledger_and_never_reaches_provider_twice(
    oauth: OAuthWorld,
) -> None:
    begun = oauth.begin().json()
    landed = oauth.provider.consent(begun["authorize_url"], email=EMAIL)
    assert oauth.complete({"redirect_url": landed}).status_code == 200
    posts_after_first = len(oauth.provider.posts)
    replay = oauth.complete({"redirect_url": landed})
    assert replay.status_code == 400
    exchange_posts = [
        post for post in oauth.provider.posts if (post.form or {}).get("code") is not None
    ]
    assert len(exchange_posts) == 1
    assert len(oauth.provider.posts) == posts_after_first


def test_redirect_uri_cannot_be_steered_by_host_header(oauth: OAuthWorld) -> None:
    begun = oauth.begin(extra_headers={"host": "evil.example"})
    assert begun.status_code == 200
    redirect = parse_qs(urlsplit(begun.json()["authorize_url"]).query)["redirect_uri"][0]
    assert redirect == REDIRECT
    evil_landing = f"http://evil.example/oauth/callback?state={begun.json()['state']}&code=x"
    assert oauth.complete({"redirect_url": evil_landing}).status_code == 400


def test_wrong_account_consent_cannot_rebind_a_connection(oauth: OAuthWorld) -> None:
    begun = oauth.begin().json()
    landed = oauth.provider.consent(begun["authorize_url"], email="hello@joshuaschultz.com")
    refused = oauth.complete({"redirect_url": landed})
    assert refused.status_code == 400
    assert "hello@" not in refused.text
    assert oauth.custody("refresh_token") is None
    assert oauth.provider.revoked, "the refused grant is revoked at the provider"


def test_scope_downgrade_is_needs_you_not_silent(oauth: OAuthWorld) -> None:
    begun = oauth.begin().json()
    landed = oauth.provider.consent(
        begun["authorize_url"], email=EMAIL, granted_scope="openid email"
    )
    refused = oauth.complete({"redirect_url": landed})
    assert refused.status_code == 400
    assert oauth.custody("refresh_token") is None
    rows = oauth.client.get("/api/connections", headers=oauth.headers(token="viewer")).json()
    row = next(item for item in rows["connections"] if item["instance"] == "blackarc")
    assert row["status"] == "needs_you"
    assert row["reason_code"] == "scope_missing"


def test_viewer_cannot_begin_complete_or_set_app(oauth: OAuthWorld) -> None:
    viewer = oauth.headers(token="viewer")
    client = oauth.client
    assert (
        client.post("/api/connections/blackarc/oauth/begin", json={}, headers=viewer).status_code
        == 403
    )
    assert (
        client.post(
            "/api/oauth/complete", json={"state": "s", "code": "c"}, headers=viewer
        ).status_code
        == 403
    )
    assert (
        client.put(
            "/api/oauth-apps/google", json={"client_id": "x", "client_secret": "y"}, headers=viewer
        ).status_code
        == 403
    )
    assert client.get("/api/oauth-apps/google", headers=viewer).status_code == 200


def test_app_secret_never_returned(oauth: OAuthWorld) -> None:
    answer = oauth.client.get("/api/oauth-apps/google", headers=oauth.headers())
    assert oauth.provider.client_secret not in answer.text
    assert len(answer.json()["client_id_hint"]) == 12


def test_provider_html_in_error_is_not_rendered(oauth: OAuthWorld) -> None:
    begun = oauth.begin().json()
    state = _state(begun["authorize_url"])
    hostile = f"{REDIRECT}?state={state}&error=%3Cscript%3Ealert(1)%3C%2Fscript%3E"
    refused = oauth.complete({"redirect_url": hostile})
    assert refused.status_code == 400
    assert "<script>" not in refused.text


@pytest.mark.parametrize(
    "pasted",
    [
        REDIRECT + "?state=s&code=c" + "&scope=" + "a" * 5000,
        REDIRECT + "?state=s&code=c%0Aevil",
        REDIRECT + "?state=s&code=c&code=d",
    ],
)
def test_oversized_control_and_duplicate_pasted_addresses_are_refused(
    oauth: OAuthWorld, pasted: str
) -> None:
    oauth.begin()
    assert oauth.complete({"redirect_url": pasted}).status_code == 400
    assert not oauth.provider.posts


def test_complete_is_rate_limited_per_session(oauth: OAuthWorld) -> None:
    for _ in range(10):
        oauth.complete({"state": "nope", "code": "nope"})
    assert oauth.complete({"state": "nope", "code": "nope"}).status_code == 429
    assert oauth.complete({"state": "nope", "code": "nope"}, session="other").status_code == 400
