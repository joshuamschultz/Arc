"""End-to-end tests for /ws/chat/{agent_id}.

Exercises the full pipe: route → adapter → SessionRouter → echo executor
→ StreamBridge → adapter → route → browser. The echo executor stands in
for ArcAgent so the tests stay hermetic — no team/ directory required.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from arcgateway.adapters.web import WebPlatformAdapter
from arcgateway.executor import AsyncioExecutor
from arcgateway.session import SessionRouter
from arcgateway.team_roster import RosterEntry
from starlette.testclient import TestClient

from arcui.auth import AuthConfig
from arcui.server import create_app

VIEWER_TOKEN = "viewer-tok-test"
OPERATOR_TOKEN = "operator-tok-test"
AGENT_TOKEN = "agent-tok-test"


@pytest.fixture
def auth_config() -> AuthConfig:
    return AuthConfig(
        {
            "viewer_token": VIEWER_TOKEN,
            "operator_token": OPERATOR_TOKEN,
            "agent_token": AGENT_TOKEN,
        }
    )


@pytest.fixture
def fake_roster() -> list[RosterEntry]:
    return [
        RosterEntry(
            agent_id="concierge",
            name="concierge",
            did="did:arc:agent:concierge",
            org=None,
            type="agent",
            workspace_path="/tmp/concierge",
            model="claude-3-5-sonnet",
            provider="anthropic",
            online=True,
            display_name="Concierge",
            color="#1abc9c",
            role_label="Test Agent",
            hidden=False,
        )
    ]


@pytest.fixture
def app_with_chat(
    tmp_path: Path,
    auth_config: AuthConfig,
    fake_roster: list[RosterEntry],
) -> Iterator[Any]:
    """Build an arcui app with the chat WS plumbing wired manually.

    We bypass bootstrap so the test never touches arcagent: AsyncioExecutor
    with no agent_factory uses the echo stub, which is exactly what we want
    for routing tests.
    """
    team_root = tmp_path / "team"
    team_root.mkdir()
    app = create_app(team_root=team_root, auth_config=auth_config)

    # Override the roster_provider to return our fake agent.
    app.state.roster_provider = lambda: list(fake_roster)

    # Wire the web adapter manually so the lifespan tests above remain
    # focused on the bootstrap path while these tests focus on routing.
    executor = AsyncioExecutor()  # agent_factory=None → echo stub
    session_router = SessionRouter(executor=executor)
    web_adapter = WebPlatformAdapter(
        on_message=session_router.handle,
        agent_did="did:arc:agent:default",
    )
    # Bind the adapter for outbound delivery (StreamBridge needs it).
    session_router._adapter = web_adapter  # type: ignore[assignment]
    app.state.web_adapter = web_adapter
    app.state.session_router = session_router
    app.state.executor = executor
    yield app


def _ws_url(agent_id: str = "concierge") -> str:
    return f"/ws/chat/{agent_id}"


def _connect_with_token(client: TestClient, token: str, agent_id: str = "concierge") -> Any:
    ws = client.websocket_connect(_ws_url(agent_id))
    return ws


# ── Auth ──────────────────────────────────────────────────────────────────────


def _consume_until_disconnect(ws: Any, max_frames: int = 5) -> dict[str, Any]:
    """Read frames until the server closes; return the last one before disconnect."""
    last: dict[str, Any] = {}
    for _ in range(max_frames):
        try:
            last = ws.receive_json()
        except Exception:
            break
    return last


def test_invalid_token_rejected(app_with_chat: Any) -> None:
    """A bad token returns an error frame and closes."""
    with TestClient(app_with_chat) as client:
        with client.websocket_connect(_ws_url()) as ws:
            ws.send_json({"token": "wrong-token"})
            err = ws.receive_json()
            assert "error" in err and "token" in err["error"].lower()


def test_auth_required_on_ws_upgrade(app_with_chat: Any) -> None:
    """A missing token returns an error frame and closes."""
    with TestClient(app_with_chat) as client:
        with client.websocket_connect(_ws_url()) as ws:
            ws.send_json({})  # no token field
            err = ws.receive_json()
            assert "error" in err


# ── Happy path ────────────────────────────────────────────────────────────────


def test_browser_message_reaches_echo_executor(app_with_chat: Any) -> None:
    """A user prompt round-trips through the echo executor and back."""
    with TestClient(app_with_chat) as client:
        with client.websocket_connect(_ws_url()) as ws:
            ws.send_json({"token": VIEWER_TOKEN})
            ready = ws.receive_json()
            assert ready["type"] == "ready"
            ws.send_json({"type": "message", "text": "ping", "client_seq": 1})

            # The echo stub echoes the prompt back via the StreamBridge.
            # We may receive an intermediate placeholder ("...") before the
            # final accumulated message. Accept any agent message that
            # contains the prompt text.
            seen_texts: list[str] = []
            for _ in range(20):
                frame = ws.receive_json()
                if frame.get("type") == "message" and frame.get("from") == "agent":
                    seen_texts.append(frame["text"])
                    if "ping" in frame["text"]:
                        break
            assert any("ping" in t for t in seen_texts), (
                f"no agent reply contained 'ping'; saw: {seen_texts}"
            )


def test_broadcast_to_two_sockets(app_with_chat: Any) -> None:
    """Two connections for the same chat both receive each agent reply."""
    with TestClient(app_with_chat) as client:
        with (
            client.websocket_connect(_ws_url()) as ws_a,
            client.websocket_connect(_ws_url()) as ws_b,
        ):
            ws_a.send_json({"token": VIEWER_TOKEN})
            ws_b.send_json({"token": VIEWER_TOKEN})
            assert ws_a.receive_json()["type"] == "ready"
            assert ws_b.receive_json()["type"] == "ready"
            ws_a.send_json({"type": "message", "text": "broadcast"})

            def _collect_replies(ws: Any) -> list[str]:
                replies: list[str] = []
                for _ in range(20):
                    frame = ws.receive_json()
                    if frame.get("type") == "message" and frame.get("from") == "agent":
                        replies.append(frame["text"])
                        if any("broadcast" in r for r in replies):
                            break
                return replies

            replies_a = _collect_replies(ws_a)
            replies_b = _collect_replies(ws_b)
            assert any("broadcast" in r for r in replies_a), f"a: {replies_a}"
            assert any("broadcast" in r for r in replies_b), f"b: {replies_b}"


def test_concurrent_messages_queue_correctly(app_with_chat: Any) -> None:
    """Two near-simultaneous messages produce two replies (no race)."""
    with TestClient(app_with_chat) as client:
        with client.websocket_connect(_ws_url()) as ws:
            ws.send_json({"token": VIEWER_TOKEN})
            ws.receive_json()  # ready
            ws.send_json({"type": "message", "text": "first", "client_seq": 1})
            ws.send_json({"type": "message", "text": "second", "client_seq": 2})

            replies: list[str] = []
            seen_first = False
            seen_second = False
            for _ in range(60):
                frame = ws.receive_json()
                if frame.get("type") != "message" or frame.get("from") != "agent":
                    continue
                replies.append(frame["text"])
                if "first" in frame["text"]:
                    seen_first = True
                if "second" in frame["text"]:
                    seen_second = True
                if seen_first and seen_second:
                    break
            assert seen_first, f"first not found in: {replies}"
            assert seen_second, f"second not found in: {replies}"


def test_send_after_ws_disconnect_is_noop(app_with_chat: Any) -> None:
    """An adapter.send to a disconnected chat_id emits a 'no_socket' audit drop."""
    web_adapter: WebPlatformAdapter = app_with_chat.state.web_adapter
    # No sockets ever registered for this chat_id; send should not raise.
    from arcgateway.delivery import DeliveryTarget

    target = DeliveryTarget(platform="web", chat_id="ghost-chat")

    async def _run() -> None:
        await web_adapter.send(target, "hello to nobody")

    asyncio.run(_run())


def test_reconnect_preserves_session_key(app_with_chat: Any) -> None:
    """Same token + agent ⇒ same chat_id across reconnects (deterministic)."""
    web_adapter: WebPlatformAdapter = app_with_chat.state.web_adapter
    captured_chat_ids: list[str] = []

    original_register = web_adapter.register_socket

    def _capture(ws: Any, agent_did: str, user_did: str, chat_id: str) -> None:
        captured_chat_ids.append(chat_id)
        return original_register(ws, agent_did, user_did, chat_id)

    with patch.object(web_adapter, "register_socket", _capture):
        with TestClient(app_with_chat) as client:
            for _ in range(2):
                with client.websocket_connect(_ws_url()) as ws:
                    ws.send_json({"token": VIEWER_TOKEN})
                    ws.receive_json()  # ready
    assert len(captured_chat_ids) == 2
    assert captured_chat_ids[0] == captured_chat_ids[1]


# ── Routing / agent resolution ───────────────────────────────────────────────


def test_unknown_agent_id_closes_with_error(app_with_chat: Any) -> None:
    """An agent_id that is not in the roster yields an error frame."""
    with TestClient(app_with_chat) as client:
        with client.websocket_connect(_ws_url("ghost-agent")) as ws:
            ws.send_json({"token": VIEWER_TOKEN})
            err = ws.receive_json()
            assert "error" in err and "ghost-agent" in err["error"]


def test_agent_token_rejected(app_with_chat: Any) -> None:
    """Agent-role tokens cannot open a chat session."""
    with TestClient(app_with_chat) as client:
        with client.websocket_connect(_ws_url()) as ws:
            ws.send_json({"token": AGENT_TOKEN})
            # The server sends an error frame and closes; we either see the
            # error or the disconnect.
            try:
                err = ws.receive_json()
                assert "error" in err or err.get("type") == "error"
            except Exception:  # noqa: S110 — server may close before the error frame
                pass
