"""P18-3.M — the Microsoft sign-in app slot and connect through the real routes.

A viewer cannot set the app, begin or finish a connect; the secret is never read
back; a cloud or tenant that could point a token anywhere else is refused at the
route. The real ``microsoft365`` bundle, only Entra's wire faked.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from packages.arcagent.tests.microsoft_fakes import TENANT, FakeEntra
from packages.arcui.tests.test_connectors_routes import _agent, _bundles, _headers, world

__all__ = ["world"]

REPO = Path(__file__).resolve().parents[4]
REDIRECT = "http://127.0.0.1:8420/oauth/callback"


@pytest.fixture
def client(world: Path) -> Any:
    shutil.copytree(
        REPO / "extensions" / "microsoft365",
        _bundles(world) / "microsoft365",
        ignore=shutil.ignore_patterns("__pycache__", "tests"),
    )
    test_client, _name, _dir = _agent(world)
    test_client.app.state.oauth_token_post = FakeEntra().post
    test_client.app.state.oauth_redirect_uri = REDIRECT
    return test_client


def _app_body(**overrides: str) -> dict[str, str]:
    entra = FakeEntra()
    return {
        "client_id": entra.client_id,
        "client_secret": entra.client_secret,
        "tenant_id": TENANT,
        "cloud": "global",
        **overrides,
    }


def test_viewer_cannot_set_the_app_or_connect(client: Any) -> None:
    viewer = _headers("viewer")
    assert (
        client.put("/api/oauth-apps/microsoft", json=_app_body(), headers=viewer).status_code
        == 403
    )
    assert client.get("/api/oauth-apps/microsoft", headers=viewer).json()["configured"] is False
    begun = client.post("/api/connections/work_mail/oauth/begin", json={}, headers=viewer)
    assert begun.status_code == 403
    done = client.post(
        "/api/oauth/complete", json={"redirect_url": f"{REDIRECT}?state=s&code=c"}, headers=viewer
    )
    assert done.status_code == 403


def test_the_secret_is_never_read_back(client: Any) -> None:
    body = _app_body()
    assert (
        client.put("/api/oauth-apps/microsoft", json=body, headers=_headers()).status_code == 200
    )
    shown = client.get("/api/oauth-apps/microsoft", headers=_headers("viewer"))
    assert shown.json()["configured"] is True
    assert shown.json()["tenant_id"] == TENANT and shown.json()["cloud"] == "global"
    assert body["client_secret"] not in shown.text


@pytest.mark.parametrize(
    "overrides",
    [
        {"cloud": "login.evil.example"},
        {"cloud": "https://graph.evil.example"},
        {"tenant_id": "common"},
        {"tenant_id": "agency.onmicrosoft.com"},
        {"tenant_id": ""},
    ],
)
def test_route_refuses_a_cloud_or_tenant_that_could_point_anywhere(
    client: Any, overrides: dict[str, str]
) -> None:
    refused = client.put(
        "/api/oauth-apps/microsoft", json=_app_body(**overrides), headers=_headers()
    )
    assert refused.status_code == 400
    assert (
        client.get("/api/oauth-apps/microsoft", headers=_headers()).json()["configured"] is False
    )
