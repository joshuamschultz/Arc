"""Unit tests for GatewayChatClient — the WS transport onto a served agent."""

from __future__ import annotations

import json

import pytest

from arctui.gateway_client import (
    GatewayAuthError,
    GatewayChatClient,
    _to_ws_url,
)
from arctui.transport import ChatTransport, TurnEvent


class _FakeWS:
    """Scripted WebSocket double: replays *incoming* frames, records sends."""

    def __init__(self, incoming: list[str]) -> None:
        self.incoming = list(incoming)
        self.sent: list[str] = []
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        if not self.incoming:
            raise AssertionError("recv() called with no scripted frames left")
        return self.incoming.pop(0)

    async def close(self) -> None:
        self.closed = True


def _client(
    incoming: list[str], *, token: str = "tok"  # noqa: S107 — test fixture token, not a secret
) -> tuple[GatewayChatClient, list[str]]:
    ws = _FakeWS(incoming)
    seen_urls: list[str] = []

    async def _connect(url: str) -> _FakeWS:
        seen_urls.append(url)
        return ws

    client = GatewayChatClient(
        "http://127.0.0.1:8420", "employee", token, connect=_connect
    )
    return client, seen_urls


# --------------------------------------------------------------------------- #
# URL mapping
# --------------------------------------------------------------------------- #


def test_to_ws_url_maps_http_to_ws_with_chat_path() -> None:
    assert (
        _to_ws_url("http://127.0.0.1:8420", "employee")
        == "ws://127.0.0.1:8420/ws/chat/employee"
    )


def test_to_ws_url_maps_https_to_wss_and_strips_trailing_slash() -> None:
    assert (
        _to_ws_url("https://host:9000/", "coder")
        == "wss://host:9000/ws/chat/coder"
    )


def test_to_ws_url_quotes_agent_id() -> None:
    assert _to_ws_url("http://h:1", "a/b") == "ws://h:1/ws/chat/a%2Fb"


# --------------------------------------------------------------------------- #
# Protocol conformance
# --------------------------------------------------------------------------- #


def test_client_satisfies_chat_transport_protocol() -> None:
    client, _ = _client([])
    assert isinstance(client, ChatTransport)


# --------------------------------------------------------------------------- #
# Handshake
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_connect_sends_token_then_consumes_ready() -> None:
    client, urls = _client([json.dumps({"type": "ready", "chat_id": "sk"})])
    await client.connect()

    assert urls == ["ws://127.0.0.1:8420/ws/chat/employee"]
    # First frame the client sends is the auth handshake.
    assert json.loads(client._ws.sent[0]) == {"token": "tok"}  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_connect_raises_on_non_ready_handshake() -> None:
    client, _ = _client([json.dumps({"error": "Invalid token"})])
    with pytest.raises(GatewayAuthError):
        await client.connect()


# --------------------------------------------------------------------------- #
# Turns
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_send_turn_sends_message_and_yields_reply_then_done() -> None:
    client, _ = _client(
        [
            json.dumps({"type": "ready", "chat_id": "sk"}),
            json.dumps({"type": "message", "from": "agent", "text": "hello there"}),
        ]
    )
    await client.connect()

    events = [ev async for ev in client.send_turn("hi")]

    assert events == [TurnEvent("message", "hello there"), TurnEvent("done")]
    # The user turn went out as a message frame.
    assert json.loads(client._ws.sent[-1]) == {"type": "message", "text": "hi"}  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_send_turn_ignores_non_agent_frames_until_reply() -> None:
    client, _ = _client(
        [
            json.dumps({"type": "ready", "chat_id": "sk"}),
            json.dumps({"type": "ping"}),
            json.dumps({"type": "message", "from": "system", "text": "noise"}),
            json.dumps({"type": "message", "from": "agent", "text": "real"}),
        ]
    )
    await client.connect()

    events = [ev async for ev in client.send_turn("hi")]
    assert events == [TurnEvent("message", "real"), TurnEvent("done")]


@pytest.mark.asyncio
async def test_send_turn_surfaces_error_frame() -> None:
    client, _ = _client(
        [
            json.dumps({"type": "ready", "chat_id": "sk"}),
            json.dumps({"type": "error", "code": "malformed", "message": "bad"}),
        ]
    )
    await client.connect()

    events = [ev async for ev in client.send_turn("hi")]
    assert events == [TurnEvent("error", "bad"), TurnEvent("done")]


@pytest.mark.asyncio
async def test_send_turn_before_connect_raises() -> None:
    client, _ = _client([])
    with pytest.raises(RuntimeError):
        _ = [ev async for ev in client.send_turn("hi")]


@pytest.mark.asyncio
async def test_aclose_closes_socket_and_is_idempotent() -> None:
    client, _ = _client([json.dumps({"type": "ready", "chat_id": "sk"})])
    await client.connect()
    ws = client._ws  # type: ignore[union-attr]
    await client.aclose()
    await client.aclose()
    assert ws.closed is True
