"""Item 9 — ``GET /api/classifiers/{name}/models`` feeds the model dropdown."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes.classifiers import routes as classifier_routes


@pytest.fixture
def client() -> TestClient:
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=classifier_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    return TestClient(app)


def _get(client: TestClient, name: str, token: str | None = "viewer"):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.get(f"/api/classifiers/{name}/models", headers=headers)


def test_jev_models_listed(client: TestClient) -> None:
    from arcllm.classifiers.jev import MODELS

    resp = _get(client, "jev")
    assert resp.status_code == 200
    assert resp.json() == {"classifier": "jev", "models": list(MODELS)}


def test_unknown_classifier_is_404(client: TestClient) -> None:
    assert _get(client, "nope").status_code == 404


def test_malformed_name_is_404_not_500(client: TestClient) -> None:
    assert _get(client, "os:system").status_code == 404


def test_requires_auth(client: TestClient) -> None:
    assert _get(client, "jev", token=None).status_code == 401
