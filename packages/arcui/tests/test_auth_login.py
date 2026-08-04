"""Signing in is what turns 'the operator token did it' into 'a person did it'."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from arcui.auth import AuthConfig
from arcui.server import create_app

GOOD = "correct-horse-battery"


@pytest.fixture
def users(tmp_path, monkeypatch):
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    from arctrust.users import OPERATOR, UserStore

    store = UserStore(tmp_path / "users.json")
    store.add("boss@example.com", GOOD, roles=(OPERATOR,))
    store.add("watcher@example.com", GOOD)
    return store


@pytest.fixture
def auth():
    return AuthConfig({"viewer_token": "v" * 64, "operator_token": "o" * 64})


@pytest.fixture
def client(auth, users):
    return TestClient(create_app(auth_config=auth))


def test_login_returns_a_session_that_names_the_person(client):
    resp = client.post(
        "/api/auth/login", json={"email": "boss@example.com", "password": GOOD}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["role"] == "operator"
    assert body["email"] == "boss@example.com"
    assert body["did"].startswith("did:arc:")
    assert len(body["token"]) > 20


def test_a_viewer_logs_in_as_a_viewer(client):
    body = client.post(
        "/api/auth/login", json={"email": "watcher@example.com", "password": GOOD}
    ).json()
    assert body["role"] == "viewer"


def test_the_session_works_on_a_protected_route(client):
    token = client.post(
        "/api/auth/login", json={"email": "boss@example.com", "password": GOOD}
    ).json()["token"]
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["anonymous"] is False
    assert body["email"] == "boss@example.com"


def test_the_static_token_still_works_but_names_nobody(client):
    """Break-glass has to keep working, and has to be visibly anonymous."""
    resp = client.get("/api/auth/me", headers={"Authorization": "Bearer " + "o" * 64})
    assert resp.status_code == 200
    body = resp.json()
    assert body["anonymous"] is True
    assert body["email"] is None
    assert body["role"] == "operator"


def test_a_wrong_password_is_refused(client):
    resp = client.post(
        "/api/auth/login", json={"email": "boss@example.com", "password": "nope-nope-nope"}
    )
    assert resp.status_code == 401


def test_an_unknown_account_answers_exactly_like_a_wrong_password(client):
    """Different wording here would let anyone enumerate who has an account."""
    wrong = client.post(
        "/api/auth/login", json={"email": "boss@example.com", "password": "nope-nope-nope"}
    )
    unknown = client.post(
        "/api/auth/login", json={"email": "ghost@example.com", "password": "nope-nope-nope"}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_repeated_failures_get_locked_out(client):
    for _ in range(5):
        client.post(
            "/api/auth/login", json={"email": "boss@example.com", "password": "wrong-wrong-wrong"}
        )
    resp = client.post(
        "/api/auth/login", json={"email": "boss@example.com", "password": GOOD}
    )
    assert resp.status_code == 429


def test_logging_out_kills_the_session(client):
    token = client.post(
        "/api/auth/login", json={"email": "boss@example.com", "password": GOOD}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/agents", headers=headers).status_code == 401


def test_login_itself_needs_no_credentials(client):
    """Otherwise there is no way to ever obtain one."""
    resp = client.post("/api/auth/login", json={})
    assert resp.status_code == 400  # a bad request, not a 401


def test_the_login_screen_can_ask_whether_accounts_exist(client):
    assert client.get("/api/auth/mode").json() == {"login_available": True}


def test_a_fresh_install_reports_no_accounts(auth, tmp_path, monkeypatch):
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "empty"))
    fresh = TestClient(create_app(auth_config=auth))
    assert fresh.get("/api/auth/mode").json() == {"login_available": False}


def test_an_unauthenticated_request_is_still_refused(client):
    assert client.get("/api/agents").status_code == 401
