"""POST /api/gateway/restart — operator-gated gateway restart.

The spawn is patched to a recorder so the test never actually restarts anything: a viewer
is refused (403) and nothing is spawned; an operator gets 200 and the configured command
(honoring ARC_RESTART_COMMAND) is what would run.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware, SessionTracker
from arcui.routes import gateway as gateway_routes


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, list[str]]:
    spawned: list[str] = []
    monkeypatch.setattr(gateway_routes, "_spawn_restart", lambda cmd: spawned.append(cmd))
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "op"})
    app = Starlette(routes=[*gateway_routes.routes])
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.audit = UIAuditLogger(enabled=False)
    app.state.session_tracker = SessionTracker()
    return TestClient(app), spawned


def test_viewer_refused_nothing_spawned(client: tuple[TestClient, list[str]]) -> None:
    c, spawned = client
    r = c.post("/api/gateway/restart", headers={"Authorization": "Bearer viewer"})
    assert r.status_code == 403
    assert spawned == []


def test_operator_triggers_default_restart(client: tuple[TestClient, list[str]]) -> None:
    c, spawned = client
    r = c.post("/api/gateway/restart", headers={"Authorization": "Bearer op"})
    assert r.status_code == 200
    assert r.json()["restarting"] is True
    assert spawned == ["systemctl --user restart --no-block arc.service"]


def test_operator_honors_custom_command(
    client: tuple[TestClient, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    c, spawned = client
    monkeypatch.setenv("ARC_RESTART_COMMAND", "sudo systemctl restart arc")
    r = c.post("/api/gateway/restart", headers={"Authorization": "Bearer op"})
    assert r.status_code == 200
    assert spawned == ["sudo systemctl restart arc"]
