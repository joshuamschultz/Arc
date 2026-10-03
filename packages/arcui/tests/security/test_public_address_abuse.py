"""J1-2 abuse cases — the OAuth redirect follows the operator's setting and nothing else.

* A spoofed ``Host`` / ``X-Forwarded-Host`` never moves the redirect, on the
  settings read or on a real sign-in begin.
* A non-https public address is refused at the enterprise and federal tiers, both
  when saved and when a tier is raised after it was saved (the next sign-in
  refuses instead of using it).
* A callback whose ``state`` does not match a begun sign-in is refused.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import arcagent
import pytest
from packages.arcui.tests.test_oauth_routes import EMAIL, OAuthWorld, world
from packages.arcui.tests.ui_settings_support import (
    OPERATOR,
    VIEWER,
    isolate,
    set_tier,
    settings_client,
)

from arcui.public_address import PublicAddress

__all__ = ["world"]

SPOOF = {"Host": "evil.example", "X-Forwarded-Host": "evil.example", "X-Forwarded-Proto": "https"}


@pytest.fixture
def oauth_world(world: Path) -> OAuthWorld:
    ready = OAuthWorld(world)
    ready.install()
    ready.set_app()
    return ready


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return isolate(tmp_path, monkeypatch)


def test_host_header_spoof_never_changes_the_redirect(config: Path) -> None:
    client, _ = settings_client()
    client.put(
        "/api/settings/public-address",
        json={"public_base_url": "https://arc.example.com"},
        headers=OPERATOR,
    )
    body = client.get("/api/settings/public-address", headers={**VIEWER, **SPOOF}).json()
    assert body["redirect_uri"] == "https://arc.example.com/oauth/callback"
    assert "evil" not in str(body)


def test_host_header_spoof_with_no_address_stays_loopback(config: Path) -> None:
    client, _ = settings_client()
    body = client.get("/api/settings/public-address", headers={**VIEWER, **SPOOF}).json()
    assert body["redirect_uri"] == "http://127.0.0.1:8420/oauth/callback"


@pytest.mark.parametrize(
    ("fleet", "gateway"),
    [("enterprise", None), ("federal", None), (None, "enterprise"), (None, "federal")],
)
@pytest.mark.parametrize("address", ["http://127.0.0.1:8420", "http://arc.example.com"])
def test_non_https_address_refused_above_personal(
    config: Path, fleet: str | None, gateway: str | None, address: str
) -> None:
    set_tier(config, fleet=fleet, gateway=gateway)
    client, audit = settings_client()
    answer = client.put(
        "/api/settings/public-address", json={"public_base_url": address}, headers=OPERATOR
    )
    assert answer.status_code == 400
    assert audit.events[-1]["outcome"] == "denied"
    gateway = config / "gateway.toml"
    assert not gateway.exists() or "public_base_url" not in gateway.read_text()


def test_tier_raised_after_saving_http_refuses_the_next_sign_in(config: Path) -> None:
    client, _ = settings_client()
    client.put(
        "/api/settings/public-address",
        json={"public_base_url": "http://127.0.0.1:8420"},
        headers=OPERATOR,
    )
    (config / "arcagent.toml").write_text('[security]\ntier = "enterprise"\n')
    with pytest.raises(arcagent.ExtensionError) as refused:
        PublicAddress(ui_port=8420).redirect_uri()
    assert refused.value.code == "PUBLIC_ADDRESS_INVALID"


def test_spoofed_host_on_begin_uses_the_saved_address(oauth_world: OAuthWorld) -> None:
    oauth_world.set_public_address("https://arc.example.com")
    begun = oauth_world.begin(extra_headers=SPOOF)
    assert begun.status_code == 200, begun.text
    redirect = parse_qs(urlsplit(begun.json()["authorize_url"]).query)["redirect_uri"]
    assert redirect == ["https://arc.example.com/oauth/callback"]


def test_callback_with_a_mismatched_state_is_refused(oauth_world: OAuthWorld) -> None:
    oauth_world.set_public_address("https://arc.example.com")
    oauth_world.begin()
    forged = "https://arc.example.com/oauth/callback?state=forged-state-value&code=4/0x"
    refused = oauth_world.complete({"redirect_url": forged})
    assert refused.status_code == 400
    assert oauth_world.custody("refresh_token") is None
    assert oauth_world.provider.exchanges == 0


def test_callback_at_the_old_loopback_is_refused_after_the_address_moves(
    oauth_world: OAuthWorld,
) -> None:
    """A code minted for the old redirect is not accepted once the address changed."""
    begun = oauth_world.begin().json()
    landed = oauth_world.provider.consent(begun["authorize_url"], email=EMAIL)
    oauth_world.set_public_address("https://arc.example.com")
    refused = oauth_world.complete({"redirect_url": landed})
    assert refused.status_code == 400
    assert oauth_world.custody("refresh_token") is None


def test_address_invalid_at_a_raised_tier_refuses_sign_in_not_the_card(
    oauth_world: OAuthWorld,
) -> None:
    """http saved at personal, then the gateway tier is raised: sign-in refuses, listing works."""
    oauth_world.set_public_address("http://127.0.0.1:8420")
    gateway = oauth_world.world / "arc" / "config" / "gateway.toml"
    gateway.write_text('[gateway]\ntier = "enterprise"\n' + gateway.read_text())
    refused = oauth_world.begin()
    assert refused.status_code == 400
    assert "public address" in refused.json()["error"]
    card = oauth_world.client.get("/api/oauth-apps/google", headers=oauth_world.headers())
    assert card.status_code == 400
    listed = oauth_world.client.get("/api/connections", headers=oauth_world.headers())
    assert listed.status_code == 200
