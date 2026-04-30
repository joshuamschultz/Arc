"""WebSocket protocol tests for ``subscribe:agent`` / ``unsubscribe:agent``.

Spec: PLAN 3.1, SDD §4.8.

Browser client sends ``{type: "subscribe:agent", agent_id}`` after auth. The
server:

1. Resolves the agent root via ``app.state.roster_provider``.
2. Calls ``WatcherManager.subscribe(agent_id, agent_root)`` (ref-counted).
3. Registers (queue, agent_id) on the FileChangeBridge.
4. Replays recent events for that agent so the browser is up-to-date instantly.

``unsubscribe:agent`` reverses each of those steps. Closing the WS drops every
subscription belonging to that queue (cleanup invariant).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from arcgateway.file_events import FileChangeEvent
from arcgateway.team_roster import RosterEntry
from starlette.testclient import TestClient

from arcui.auth import AuthConfig
from arcui.server import create_app


def _roster_with(agent_id: str, root: Path) -> list[RosterEntry]:
    return [
        RosterEntry(
            agent_id=agent_id,
            name=agent_id,
            did=f"did:arc:test:{agent_id}",
            org=None,
            type=None,
            workspace_path=str(root),
            model=None,
            provider=None,
            online=True,
            display_name=agent_id,
            color="#aaaaaa",
            role_label="",
            hidden=False,
        )
    ]


def _setup_app(tmp_path: Path, agent_id: str = "alpha") -> tuple[Any, TestClient]:
    """Build a test app with the agent fleet pre-injected."""
    auth = AuthConfig({"viewer_token": "ws-viewer", "operator_token": "op"})
    agent_root = tmp_path / f"{agent_id}_agent"
    agent_root.mkdir(parents=True, exist_ok=True)
    (agent_root / "workspace").mkdir(exist_ok=True)

    app = create_app(auth_config=auth, team_root=tmp_path)
    # Override roster to deterministic single-entry list.
    app.state.roster_provider = lambda: _roster_with(agent_id, agent_root)
    return app, TestClient(app)


def _auth(ws: Any) -> None:
    ws.send_text(json.dumps({"token": "ws-viewer"}))
    resp = ws.receive_json()
    assert resp["type"] == "auth_ok"


class TestSubscribeAgent:
    def test_subscribe_calls_watcher_manager(self, tmp_path: Path) -> None:
        app, client = _setup_app(tmp_path)
        with client.websocket_connect("/ws") as ws:
            _auth(ws)
            ws.send_text(json.dumps({"type": "subscribe:agent", "agent_id": "alpha"}))
            # Trigger a no-op broadcast as a barrier to flush the receive task.
            app.state.connection_manager.broadcast({"type": "ping"})
            ws.receive_json()  # consume ping

            # Watcher refcount went 0 → 1.
            assert app.state.watcher_manager.refcount("alpha") == 1

    def test_subscribe_unknown_agent_emits_error(self, tmp_path: Path) -> None:
        _, client = _setup_app(tmp_path)
        with client.websocket_connect("/ws") as ws:
            _auth(ws)
            ws.send_text(json.dumps({"type": "subscribe:agent", "agent_id": "ghost"}))
            err = ws.receive_json()
            assert err["type"] == "subscribe:error"
            assert err["agent_id"] == "ghost"

    def test_file_change_event_reaches_subscribed_client(self, tmp_path: Path) -> None:
        app, client = _setup_app(tmp_path)
        with client.websocket_connect("/ws") as ws:
            _auth(ws)
            ws.send_text(json.dumps({"type": "subscribe:agent", "agent_id": "alpha"}))
            # Round-trip a ping to ensure the subscribe was processed.
            app.state.connection_manager.broadcast({"type": "ping"})
            ws.receive_json()

            evt = FileChangeEvent(
                agent_id="alpha",
                event_type="policy:bullets_updated",
                path="workspace/policy.md",
                payload={"bullets": [{"id": "P01"}]},
            )
            asyncio.run(app.state.file_change_bridge.handle_event(evt))

            msg = ws.receive_json()
            assert msg["type"] == "file_change"
            assert msg["agent_id"] == "alpha"
            assert msg["event_type"] == "policy:bullets_updated"
            assert msg["payload"] == {"bullets": [{"id": "P01"}]}

    def test_file_change_for_unsubscribed_agent_is_filtered(self, tmp_path: Path) -> None:
        """A client subscribed to ``alpha`` must not receive events for ``beta``."""
        app, client = _setup_app(tmp_path)
        with client.websocket_connect("/ws") as ws:
            _auth(ws)
            ws.send_text(json.dumps({"type": "subscribe:agent", "agent_id": "alpha"}))
            app.state.connection_manager.broadcast({"type": "ping"})
            ws.receive_json()

            asyncio.run(
                app.state.file_change_bridge.handle_event(
                    FileChangeEvent(
                        agent_id="beta",
                        event_type="policy:bullets_updated",
                        path="workspace/policy.md",
                        payload={},
                    )
                )
            )

            # Send another broadcast so we have something to receive — if the
            # filter is broken, the file_change message arrives first.
            app.state.connection_manager.broadcast({"type": "barrier"})
            msg = ws.receive_json()
            assert msg["type"] == "barrier"


class TestUnsubscribeAgent:
    def test_unsubscribe_drains_watcher_refcount(self, tmp_path: Path) -> None:
        app, client = _setup_app(tmp_path)
        with client.websocket_connect("/ws") as ws:
            _auth(ws)
            ws.send_text(json.dumps({"type": "subscribe:agent", "agent_id": "alpha"}))
            app.state.connection_manager.broadcast({"type": "ping"})
            ws.receive_json()
            assert app.state.watcher_manager.refcount("alpha") == 1

            ws.send_text(json.dumps({"type": "unsubscribe:agent", "agent_id": "alpha"}))
            app.state.connection_manager.broadcast({"type": "ping2"})
            ws.receive_json()

            assert app.state.watcher_manager.refcount("alpha") == 0


class TestSubscribeReplay:
    def test_subscribe_replays_recent_events(self, tmp_path: Path) -> None:
        """Recent events for an agent are replayed on subscribe."""
        app, client = _setup_app(tmp_path)

        # Pre-load an event into the bridge ring before the client connects.
        asyncio.run(
            app.state.file_change_bridge.handle_event(
                FileChangeEvent(
                    agent_id="alpha",
                    event_type="config:updated",
                    path="arcagent.toml",
                    payload={"k": "v"},
                )
            )
        )

        with client.websocket_connect("/ws") as ws:
            _auth(ws)
            ws.send_text(json.dumps({"type": "subscribe:agent", "agent_id": "alpha"}))
            replayed = ws.receive_json()
            assert replayed["type"] == "file_change"
            assert replayed["event_type"] == "config:updated"


class TestDisconnectCleanup:
    def test_disconnect_releases_subscriptions(self, tmp_path: Path) -> None:
        app, client = _setup_app(tmp_path)
        with client.websocket_connect("/ws") as ws:
            _auth(ws)
            ws.send_text(json.dumps({"type": "subscribe:agent", "agent_id": "alpha"}))
            app.state.connection_manager.broadcast({"type": "ping"})
            ws.receive_json()
            assert app.state.watcher_manager.refcount("alpha") == 1

        # Context exit closes the WS — cleanup must drop the watcher.
        # Allow the server-side teardown coroutine a chance to settle.
        # TestClient's WS context manager is synchronous but the server
        # finally-block runs on the connection's loop; a tiny sleep on the
        # current loop is sufficient because TestClient uses a single thread.
        for _ in range(10):
            if app.state.watcher_manager.refcount("alpha") == 0:
                break
            import time

            time.sleep(0.05)
        assert app.state.watcher_manager.refcount("alpha") == 0
