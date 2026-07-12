"""``POST /api/agents/{agent_id}/sessions/new`` — session rotation endpoint.

Mirrors ``test_task_mutation_routes`` for auth/audit conventions. Rotation is
allowed for viewer AND operator (the caller's own conversation), returns the
new session key, and is audited. Unknown agent -> 404; no role -> 403.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from arcgateway.session import SessionRouter
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware

_AGENT_ID = "josh_agent"
_AGENT_DID = "did:arc:local:executor/abc123"


class _NullExecutor:
    async def run(self, event: Any) -> Any:  # pragma: no cover - never invoked
        raise AssertionError("rotation must not run a turn")


def _make_app() -> tuple[Starlette, AuthConfig, SessionRouter]:
    from arcui.routes.agent_sessions import routes

    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app = Starlette(routes=routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.audit = UIAuditLogger()
    router: SessionRouter = SessionRouter(executor=_NullExecutor())  # type: ignore[arg-type]
    app.state.session_router = router
    app.state.roster_provider = lambda: [
        SimpleNamespace(agent_id=_AGENT_ID, name=_AGENT_ID, did=_AGENT_DID)
    ]
    return app, auth, router


def _hdr(auth: AuthConfig, role: str) -> dict[str, str]:
    token = auth.operator_token if role == "operator" else auth.viewer_token
    return {"Authorization": f"Bearer {token}"}


def _mutations(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    out = []
    for record in caplog.records:
        if record.name != "arcui.audit":
            continue
        payload = json.loads(record.message)
        if payload["event_type"] == "ui.mutation":
            out.append(payload["details"])
    return out


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_rotation_allowed_and_key_changes(role: str, caplog: pytest.LogCaptureFixture) -> None:
    app, auth, _router = _make_app()
    client = TestClient(app)

    with caplog.at_level("INFO", logger="arcui.audit"):
        resp = client.post(f"/api/agents/{_AGENT_ID}/sessions/new", headers=_hdr(auth, role))

    assert resp.status_code == 201
    new_key = resp.json()["session_key"]
    assert new_key
    # A second rotation returns a different key — the epoch advanced.
    resp2 = client.post(f"/api/agents/{_AGENT_ID}/sessions/new", headers=_hdr(auth, role))
    assert resp2.json()["session_key"] != new_key

    applied = [m for m in _mutations(caplog) if m["outcome"] == "applied"]
    assert applied and applied[0]["operation"] == "session.rotate"


def test_no_token_is_unauthorized() -> None:
    """AuthMiddleware rejects an unauthenticated /api/* call before the handler."""
    app, _auth, _router = _make_app()
    client = TestClient(app)
    resp = client.post(f"/api/agents/{_AGENT_ID}/sessions/new")
    assert resp.status_code == 401


def test_unknown_agent_is_404() -> None:
    app, auth, _router = _make_app()
    client = TestClient(app)
    resp = client.post("/api/agents/ghost/sessions/new", headers=_hdr(auth, "operator"))
    assert resp.status_code == 404
