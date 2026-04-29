"""Tests for Agent WebSocket endpoint — /api/agent/connect."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig
from arcui.connection import ConnectionManager
from arcui.event_buffer import EventBuffer
from arcui.registry import AgentRegistry
from arcui.routes.agent_ws import routes as agent_ws_routes
from arcui.subscription import SubscriptionManager


def _make_app(max_agents: int = 100) -> tuple[Starlette, AuthConfig]:
    auth = AuthConfig(
        {
            "viewer_token": "viewer",
            "operator_token": "operator",
            "agent_token": "agent-secret",
        }
    )
    cm = ConnectionManager()
    registry = AgentRegistry(max_agents=max_agents)
    sub_mgr = SubscriptionManager()
    buf = EventBuffer(cm, subscription_manager=sub_mgr)

    app = Starlette(routes=agent_ws_routes)
    app.state.auth_config = auth
    app.state.connection_manager = cm
    app.state.agent_registry = registry
    app.state.subscription_manager = sub_mgr
    app.state.event_buffer = buf
    app.state.aggregator = None
    app.state.pending_controls = {}
    app.state.audit = UIAuditLogger(enabled=False)
    return app, auth


def _auth_message(token: str = "agent-secret") -> dict:  # noqa: S107
    return {
        "token": token,
        "registration": {
            "agent_name": "test-agent",
            "model": "gpt-4",
            "provider": "openai",
        },
    }


class TestAgentWSAuth:
    def test_valid_auth_returns_auth_ok(self):
        app, _ = _make_app()
        client = TestClient(app)
        with client.websocket_connect("/api/agent/connect") as ws:
            ws.send_json(_auth_message())
            resp = ws.receive_json()
            assert resp["type"] == "auth_ok"
            assert "agent_id" in resp

    def test_invalid_token_closes_4003(self):
        app, _ = _make_app()
        client = TestClient(app)
        with client.websocket_connect("/api/agent/connect") as ws:
            ws.send_json(_auth_message(token="bad-token"))
            resp = ws.receive_json()
            assert "error" in resp

    def test_agent_registered_on_auth(self):
        app, _ = _make_app()
        client = TestClient(app)
        with client.websocket_connect("/api/agent/connect") as ws:
            ws.send_json(_auth_message())
            resp = ws.receive_json()
            agent_id = resp["agent_id"]

            registry = app.state.agent_registry
            assert registry.get(agent_id) is not None

    def test_capacity_full_returns_4029(self):
        app, _ = _make_app(max_agents=0)
        client = TestClient(app)
        with client.websocket_connect("/api/agent/connect") as ws:
            ws.send_json(_auth_message())
            resp = ws.receive_json()
            assert resp.get("error") == "Server at capacity"


class TestAgentWSEventHandling:
    def test_event_received_and_buffered(self):
        app, _ = _make_app()
        client = TestClient(app)
        with client.websocket_connect("/api/agent/connect") as ws:
            ws.send_json(_auth_message())
            resp = ws.receive_json()
            assert resp["type"] == "auth_ok"

            # Send a UIEvent
            ws.send_json(
                {
                    "type": "event",
                    "payload": {
                        "layer": "llm",
                        "event_type": "trace_record",
                        "agent_id": "",
                        "agent_name": "test-agent",
                        "source_id": "call-1",
                        "timestamp": "2026-03-03T12:00:00+00:00",
                        "data": {"model": "gpt-4"},
                        "sequence": 0,
                    },
                }
            )

    def test_malformed_event_continues_connection(self):
        """A ValidationError from bad payload should not kill the WS."""
        app, _ = _make_app()
        client = TestClient(app)
        with client.websocket_connect("/api/agent/connect") as ws:
            ws.send_json(_auth_message())
            resp = ws.receive_json()
            assert resp["type"] == "auth_ok"

            # Send malformed event (missing required fields)
            ws.send_json(
                {
                    "type": "event",
                    "payload": {"bad": "data"},
                }
            )

            # Connection should still be alive — send a valid event
            ws.send_json(
                {
                    "type": "event",
                    "payload": {
                        "layer": "llm",
                        "event_type": "test_event",
                        "agent_id": "",
                        "agent_name": "test-agent",
                        "source_id": "call-1",
                        "timestamp": "2026-03-03T12:00:00+00:00",
                        "data": {},
                        "sequence": 1,
                    },
                }
            )

    def test_per_agent_aggregator_created_on_register(self):
        app, _ = _make_app()
        client = TestClient(app)
        with client.websocket_connect("/api/agent/connect") as ws:
            ws.send_json(_auth_message())
            resp = ws.receive_json()
            agent_id = resp["agent_id"]

            entry = app.state.agent_registry.get(agent_id)
            assert entry is not None
            assert entry.aggregator is not None


class TestAgentWSDisconnect:
    def test_agent_unregistered_on_disconnect(self):
        app, _ = _make_app()
        registry = app.state.agent_registry
        client = TestClient(app)

        with client.websocket_connect("/api/agent/connect") as ws:
            ws.send_json(_auth_message())
            resp = ws.receive_json()
            agent_id = resp["agent_id"]
            assert registry.get(agent_id) is not None

        # After disconnect, agent should be unregistered
        assert registry.get(agent_id) is None

    def test_disconnect_only_errors_own_pending_controls(self):
        """When agent disconnects, only its pending controls get errors."""
        import asyncio

        app, _ = _make_app()
        pending = app.state.pending_controls
        loop = asyncio.new_event_loop()

        # Create futures for two agents
        f1: asyncio.Future[object] = loop.create_future()
        f2: asyncio.Future[object] = loop.create_future()
        # Store as (target_agent_id, future) tuple
        pending["req-1"] = ("agent-other", f1)
        pending["req-2"] = ("agent-other", f2)

        client = TestClient(app)
        with client.websocket_connect("/api/agent/connect") as ws:
            ws.send_json(_auth_message())
            ws.receive_json()  # auth_ok
            # This agent connects and gets a unique ID

        # After disconnect, other agent's futures should NOT be errored
        assert not f1.done()
        assert not f2.done()

        loop.close()
