"""Tests for agents REST routes — list, detail, control proxy."""

from __future__ import annotations

from unittest.mock import MagicMock

from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware
from arcui.registry import AgentRegistry
from arcui.routes.agents import routes as agent_routes
from arcui.types import AgentRegistration


def _make_app() -> tuple[Starlette, AuthConfig, AgentRegistry]:
    auth = AuthConfig({
        "viewer_token": "viewer",
        "operator_token": "operator",
        "agent_token": "agent-secret",
    })
    registry = AgentRegistry()

    app = Starlette(routes=agent_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.agent_registry = registry
    app.state.pending_controls = {}
    app.state.audit = UIAuditLogger(enabled=False)
    return app, auth, registry


def _register_agent(
    registry: AgentRegistry,
    agent_id: str = "agent-001",
    agent_name: str = "test-agent",
) -> None:
    reg = AgentRegistration(
        agent_id=agent_id,
        agent_name=agent_name,
        model="gpt-4",
        provider="openai",
        connected_at="2026-03-03T12:00:00+00:00",
    )
    ws = MagicMock()
    registry.register(agent_id, ws, reg)


class TestListAgents:
    def test_list_empty(self):
        app, auth, _ = _make_app()
        client = TestClient(app)
        resp = client.get(
            "/api/agents",
            headers={"Authorization": f"Bearer {auth.viewer_token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["agents"] == []

    def test_list_with_agents(self):
        app, auth, registry = _make_app()
        _register_agent(registry, "a1", "agent-alpha")
        _register_agent(registry, "a2", "agent-beta")

        client = TestClient(app)
        resp = client.get(
            "/api/agents",
            headers={"Authorization": f"Bearer {auth.viewer_token}"},
        )
        assert resp.status_code == 200
        agents = resp.json()["agents"]
        assert len(agents) == 2

    def test_list_requires_auth(self):
        app, _, _ = _make_app()
        client = TestClient(app)
        resp = client.get("/api/agents")
        assert resp.status_code == 401


class TestGetAgent:
    def test_get_existing_agent(self):
        app, auth, registry = _make_app()
        _register_agent(registry, "a1", "agent-alpha")

        client = TestClient(app)
        resp = client.get(
            "/api/agents/a1",
            headers={"Authorization": f"Bearer {auth.viewer_token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["agent"]["agent_id"] == "a1"

    def test_get_nonexistent_returns_404(self):
        app, auth, _ = _make_app()
        client = TestClient(app)
        resp = client.get(
            "/api/agents/nonexistent",
            headers={"Authorization": f"Bearer {auth.viewer_token}"},
        )
        assert resp.status_code == 404


class TestControlAgent:
    def test_control_requires_operator(self):
        app, auth, registry = _make_app()
        _register_agent(registry, "a1")

        client = TestClient(app)
        resp = client.post(
            "/api/agents/a1/control",
            json={"action": "cancel", "data": {}, "request_id": "req-1"},
            headers={"Authorization": f"Bearer {auth.viewer_token}"},
        )
        assert resp.status_code == 403

    def test_control_nonexistent_agent_returns_404(self):
        app, auth, _ = _make_app()
        client = TestClient(app)
        resp = client.post(
            "/api/agents/nonexistent/control",
            json={"action": "cancel", "data": {}, "request_id": "req-1"},
            headers={"Authorization": f"Bearer {auth.operator_token}"},
        )
        assert resp.status_code == 404

    def test_control_malformed_body_400(self):
        app, auth, registry = _make_app()
        _register_agent(registry, "a1")

        client = TestClient(app)
        resp = client.post(
            "/api/agents/a1/control",
            content=b"not json",
            headers={
                "Authorization": f"Bearer {auth.operator_token}",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 400

    def test_control_missing_fields_400(self):
        app, auth, registry = _make_app()
        _register_agent(registry, "a1")

        client = TestClient(app)
        resp = client.post(
            "/api/agents/a1/control",
            json={"data": {}},  # missing action and request_id
            headers={"Authorization": f"Bearer {auth.operator_token}"},
        )
        assert resp.status_code == 400
        assert "Missing required fields" in resp.json()["error"]

    def test_control_invalid_action_400(self):
        app, auth, registry = _make_app()
        _register_agent(registry, "a1")

        client = TestClient(app)
        resp = client.post(
            "/api/agents/a1/control",
            json={"action": "invalid_action", "data": {}, "request_id": "req-1"},
            headers={"Authorization": f"Bearer {auth.operator_token}"},
        )
        assert resp.status_code == 400
