"""WebSocket route tests — auth, streaming, heartbeat, disconnect."""

from __future__ import annotations

import json

from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.auth import AuthConfig
from arcui.server import create_app


def _make_ws_app() -> tuple[Starlette, TestClient, AuthConfig]:
    """Build a test app with known auth tokens for WebSocket testing."""
    auth = AuthConfig({"viewer_token": "ws-viewer", "operator_token": "ws-operator"})
    app = create_app(auth_config=auth)
    client = TestClient(app)
    return app, client, auth


class TestWebSocketAuth:
    """WebSocket first-message auth tests."""

    def test_valid_viewer_token_auth(self) -> None:
        _, client, _ = _make_ws_app()
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"token": "ws-viewer"}))
            resp = ws.receive_json()
            assert resp["type"] == "auth_ok"
            assert resp["role"] == "viewer"

    def test_valid_operator_token_auth(self) -> None:
        _, client, _ = _make_ws_app()
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"token": "ws-operator"}))
            resp = ws.receive_json()
            assert resp["type"] == "auth_ok"
            assert resp["role"] == "operator"

    def test_invalid_token_sends_error_and_closes(self) -> None:
        _, client, _ = _make_ws_app()
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"token": "bad-token"}))
            resp = ws.receive_json()
            assert resp["error"] == "Invalid token"
            # Server closes with code 4003 — context manager handles cleanup

    def test_malformed_json_sends_error_and_closes(self) -> None:
        _, client, _ = _make_ws_app()
        with client.websocket_connect("/ws") as ws:
            ws.send_text("not-json")
            resp = ws.receive_json()
            assert "error" in resp

    def test_missing_token_field_sends_error(self) -> None:
        _, client, _ = _make_ws_app()
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"no_token": "here"}))
            resp = ws.receive_json()
            assert resp["error"] == "Invalid token"

    def test_agent_token_rejected_on_browser_ws(self) -> None:
        """Agent tokens must not access the browser /ws feed (ASI03).

        The browser WebSocket endpoint is for human operators and viewers.
        Agent tokens exist only for the /api/agent/connect endpoint.
        Accepting them here would allow an agent to read all other agents'
        events — a privilege escalation.
        """
        auth = AuthConfig({
            "viewer_token": "ws-viewer",
            "operator_token": "ws-operator",
            "agent_token": "ws-agent",
        })
        app = create_app(auth_config=auth)
        client = TestClient(app)
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"token": "ws-agent"}))
            resp = ws.receive_json()
            assert "error" in resp
            assert "Agent" in resp["error"]


class TestWebSocketEventStreaming:
    """Event streaming after successful auth."""

    def test_receives_broadcast_events(self) -> None:
        app, client, _ = _make_ws_app()
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"token": "ws-viewer"}))
            resp = ws.receive_json()
            assert resp["type"] == "auth_ok"

            # Push an event through the connection manager
            app.state.connection_manager.broadcast(
                {"type": "event_batch", "events": [{"trace_id": "test-123"}]}
            )

            # Client should receive it
            data = ws.receive_json()
            assert data["type"] == "event_batch"
            assert data["events"][0]["trace_id"] == "test-123"


class TestWebSocketSubscribe:
    """Browser subscribe message handling."""

    def test_subscribe_message_accepted(self) -> None:
        app, client, _ = _make_ws_app()
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"token": "ws-viewer"}))
            resp = ws.receive_json()
            assert resp["type"] == "auth_ok"

            # Send subscribe message
            ws.send_text(json.dumps({
                "type": "subscribe",
                "agents": ["a1"],
                "layers": ["llm"],
            }))

            # Should still receive broadcasts (subscribe is a filter, not ack)
            app.state.connection_manager.broadcast({"type": "test"})
            data = ws.receive_json()
            assert data["type"] == "test"

    def test_oversized_message_is_skipped(self) -> None:
        """DoS prevention: messages over 1 MB are silently dropped.

        An oversized message must not crash the loop or disconnect the client.
        Events sent after the oversized message must still be delivered.
        """
        from arcui.ws_helpers import MAX_WS_MESSAGE_SIZE

        app, client, _ = _make_ws_app()
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"token": "ws-viewer"}))
            resp = ws.receive_json()
            assert resp["type"] == "auth_ok"

            # Send a message that exceeds the size limit
            oversized = "x" * (MAX_WS_MESSAGE_SIZE + 1)
            ws.send_text(oversized)

            # Connection must still be alive — subsequent broadcast is received
            app.state.connection_manager.broadcast({"type": "alive_check"})
            data = ws.receive_json()
            assert data["type"] == "alive_check"
