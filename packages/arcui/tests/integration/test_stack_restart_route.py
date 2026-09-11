"""POST /api/stack/restart — operator-gated full-stack restart.

The spawn is patched to a recorder so the test never actually restarts anything:
a viewer is refused (403) and nothing is spawned; an operator gets 200 and the
launched argv is what would run — the ``--with-db`` flag rides on the request
body, and ARC_STACK_RESTART_COMMAND overrides the launcher.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware, SessionTracker
from arcui.routes import stack as stack_routes


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, list[list[str]]]:
    spawned: list[list[str]] = []
    monkeypatch.setattr(stack_routes, "_spawn_restart", lambda argv: spawned.append(argv))
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "op"})
    app = Starlette(routes=[*stack_routes.routes])
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.audit = UIAuditLogger(enabled=False)
    app.state.session_tracker = SessionTracker()
    return TestClient(app), spawned


def test_viewer_refused_nothing_spawned(client: tuple[TestClient, list[list[str]]]) -> None:
    c, spawned = client
    r = c.post("/api/stack/restart", headers={"Authorization": "Bearer viewer"})
    assert r.status_code == 403
    assert spawned == []


def test_operator_default_launcher_is_transient_systemd_unit(
    client: tuple[TestClient, list[list[str]]],
) -> None:
    c, spawned = client
    r = c.post("/api/stack/restart", headers={"Authorization": "Bearer op"})
    assert r.status_code == 200
    assert r.json()["restarting"] is True
    assert r.json()["with_db"] is False
    argv = spawned[0]
    assert argv[0] == "systemd-run"
    assert "--user" in argv
    assert argv[-1] == "restart"  # no --with-db by default
    assert "--with-db" not in argv


def test_with_db_appends_flag(client: tuple[TestClient, list[list[str]]]) -> None:
    c, spawned = client
    r = c.post(
        "/api/stack/restart",
        headers={"Authorization": "Bearer op"},
        json={"with_db": True},
    )
    assert r.status_code == 200
    assert r.json()["with_db"] is True
    assert spawned[0][-1] == "--with-db"


def test_custom_launcher_env_is_honored(
    client: tuple[TestClient, list[list[str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    c, spawned = client
    monkeypatch.setenv("ARC_STACK_RESTART_COMMAND", "ssh box arc restart")
    r = c.post(
        "/api/stack/restart",
        headers={"Authorization": "Bearer op"},
        json={"with_db": True},
    )
    assert r.status_code == 200
    assert spawned[0] == ["ssh", "box", "arc", "restart", "--with-db"]
