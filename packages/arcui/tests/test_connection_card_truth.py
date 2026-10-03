"""The connection card tells the truth and the customer finishes every journey in arcui.

Hard product rule: add, Connect or paste, healthy, expire, Reconnect, healthy, with no
terminal and no command to copy. Every request below is one the web page sends. Only the
provider wire is fake; the routes, custody, health authority and bundles are real.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from packages.arcui.tests.test_connectors_routes import (
    _EXTENSION,
    _INSTANCE,
    _SENTINEL,
    _SIGNED_IN_MARKER,
    _agent,
    _bundles,
    _connect_signin,
    _headers,
    _install,
    _write_bundle,
    world,
)
from packages.arcui.tests.test_oauth_routes import EMAIL, OAuthWorld
from starlette.testclient import TestClient

__all__ = ["world"]

OPERATOR = _headers("operator")


def _row(client: TestClient, instance: str) -> dict[str, Any]:
    listed = client.get("/api/connections", headers=_headers("viewer")).json()["connections"]
    (row,) = [r for r in listed if r["instance"] == instance]
    return dict(row)


def _check_now(client: TestClient, instance: str) -> dict[str, Any]:
    answered = client.post(f"/api/connections/{instance}/probe", headers=OPERATOR)
    assert answered.status_code == 200, answered.text
    return _row(client, instance)


@pytest.fixture
def oauth(world: Path) -> OAuthWorld:
    return OAuthWorld(world)


# --- D16: the card links straight to the missing sign-in app ------------------------


def test_an_oauth_card_with_no_sign_in_app_asks_for_the_app_first(oauth: OAuthWorld) -> None:
    oauth.install()

    row = _row(oauth.client, "blackarc")

    assert row["app_missing"] is True
    assert row["reason_text"] == "Set up the Google sign-in app first"
    assert (row["action"], row["action_label"]) == ("reconnect", "Set up Google sign-in app")


def test_once_the_app_is_set_up_the_hint_is_gone(oauth: OAuthWorld) -> None:
    oauth.install()
    oauth.set_app()

    row = _row(oauth.client, "blackarc")

    assert row["app_missing"] is False
    assert row["reason_text"] != "Set up the Google sign-in app first"


# --- D18: the catalog says whether Install can ever succeed -------------------------


def test_the_catalog_says_a_bundle_with_no_pinned_binary_is_not_auto_installable(
    world: Path,
) -> None:
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world))

    listed = client.get("/api/connectors/catalog", headers=_headers("viewer")).json()

    (entry,) = [e for e in listed["available"] if e["name"] == _EXTENSION]
    assert entry["auto_installable"] is False


def test_the_host_setup_answer_carries_an_action_code(world: Path) -> None:
    client, _agent_id, _dir = _agent(world)
    _write_bundle(_bundles(world))

    answered = client.post(f"/api/connections/{_EXTENSION}/host-setup", headers=OPERATOR)

    assert answered.status_code == 200, answered.text
    assert answered.json()["action"] == ""


# --- The journeys: add, Connect or paste, healthy, expire, Reconnect, healthy -------


def test_oauth_journey_add_connect_expire_reconnect(oauth: OAuthWorld) -> None:
    oauth.provider.access_lifetime = 1
    client = oauth.client

    oauth.install()  # Add
    assert _row(client, "blackarc")["app_missing"] is True
    oauth.set_app()  # the sign-in app form the card linked to

    def connect() -> dict[str, Any]:  # the Connect button, then the provider's page
        begun = oauth.begin()
        assert begun.status_code == 200, begun.text
        landed = oauth.provider.consent(begun.json()["authorize_url"], email=EMAIL)
        done = oauth.complete({"redirect_url": landed})
        assert done.status_code == 200, done.text
        return _row(client, "blackarc")

    assert connect()["status"] == "healthy"

    oauth.provider.revoke_all()  # the account owner removed the app
    expired = _check_now(client, "blackarc")
    assert (expired["status"], expired["action"]) == ("needs_you", "reconnect")

    assert connect()["status"] == "healthy"  # Reconnect


_TOKEN_ADAPTER = """
from __future__ import annotations

from pathlib import Path
from typing import Any

from arcagent.extension.attachment import ProbeResult, ToolResult, ToolSpec


class Keyed:
    def __init__(self, context: dict[str, Any]) -> None:
        self._credential = context["credential"]
        self._revoked = Path(str(context["bundle"])) / "revoked.txt"

    def requirements(self) -> list[Any]:
        return []

    async def probe(self) -> ProbeResult:
        token = await self._credential.maybe_field("api_token")
        value = token.reveal() if token else ""
        revoked = self._revoked.read_text().split() if self._revoked.exists() else []
        alive = bool(value) and value not in revoked
        return ProbeResult(
            reachable=alive,
            tools=await self.describe_tools(),
            detail="" if alive else "token_revoked: the provider no longer honours this token",
        )

    async def describe_tools(self) -> list[ToolSpec]:
        return [ToolSpec(name="ping", description="Ping.", classification="read_only")]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(tool=tool, content="pong")


def build_native_attachment(context: dict[str, Any]) -> Keyed:
    return Keyed(context)
"""


def test_token_journey_add_paste_expire_reconnect(world: Path) -> None:
    client, _agent_id, _dir = _agent(world)
    bundle = _write_bundle(_bundles(world))
    # Its own module name: the loader caches an entrypoint by name for the whole process.
    manifest = bundle / "extension.toml"
    manifest.write_text(
        manifest.read_text().replace("acme_attachment", "acme_keyed_attachment"), encoding="utf-8"
    )
    (bundle / "acme_keyed_attachment.py").write_text(_TOKEN_ADAPTER, encoding="utf-8")

    added = _install(client, secrets={"api_token": "token-one"})  # Add + paste
    assert added.status_code == 200, added.text
    assert _check_now(client, _INSTANCE)["status"] == "healthy"

    (bundle / "revoked.txt").write_text("token-one")  # the token expires
    expired = _check_now(client, _INSTANCE)
    assert (expired["status"], expired["action"]) == ("needs_you", "reconnect")

    # Reconnect opens the re-auth form (not the host sign-in panel) and pastes a new token.
    rotated = client.put(
        f"/api/connections/{_INSTANCE}/auth",
        json={"secrets": {"api_token": "token-two"}},
        headers=OPERATOR,
    )
    assert rotated.status_code == 200, rotated.text
    assert _row(client, _INSTANCE)["status"] == "healthy"


def test_host_login_journey_add_paste_expire_reconnect(world: Path) -> None:
    client, _agent_id, _dir, bundle = _connect_signin(world, token_login=True)
    manifest = bundle / "extension.toml"
    # What a shipped host-login bundle declares: the card asks the host if it is signed in.
    manifest.write_text(manifest.read_text() + '\n[health]\nprobe = "host_verify"\n')

    def paste_token(token: str) -> None:
        answered = client.post(
            f"/api/connections/{_INSTANCE}/authorize", json={"token": token}, headers=OPERATOR
        )
        assert answered.status_code == 200, answered.text
        assert answered.json()["sign_in"] == "signed_in"

    paste_token(_SENTINEL)  # Add (installed signed out), then paste
    assert _check_now(client, _INSTANCE)["status"] == "healthy"

    (bundle / _SIGNED_IN_MARKER).unlink()  # the host's session expires
    expired = _check_now(client, _INSTANCE)
    assert expired["status"] == "needs_you"

    paste_token(_SENTINEL)  # Reconnect
    assert _check_now(client, _INSTANCE)["status"] == "healthy"


def test_the_catalog_marks_an_oauth_bundles_refresh_token_as_connect_managed(
    oauth: OAuthWorld,
) -> None:
    """J-U1: the add form never asks the customer to type what Connect fills in."""
    listed = oauth.client.get("/api/connectors/catalog", headers=_headers("viewer")).json()

    (entry,) = [e for e in listed["available"] if e["name"] == "acme_google"]
    managed = {f["name"]: f["managed"] for f in entry["secrets"]}
    assert managed == {"account": False, "refresh_token": True}
    assert entry["oauth_provider"] == "google"
