"""Live smoke test: GatewayChatClient over a real localhost WebSocket.

Exercises the production ``_default_connect`` (the ``websockets`` asyncio
client) and the full handshake + turn round-trip against a tiny server that
speaks the same ``/ws/chat`` frame protocol as the gateway's web adapter. This
is the one path the unit-test fakes can't cover.
"""

from __future__ import annotations

import json

import pytest

from arctui.gateway_client import GatewayChatClient
from arctui.transport import TurnEvent


async def _chat_handler(websocket: object) -> None:
    """Minimal server: token handshake, then echo one agent reply per turn."""
    # Handshake: consume the token frame, acknowledge ready.
    await websocket.recv()  # type: ignore[attr-defined]
    await websocket.send(json.dumps({"type": "ready", "chat_id": "sk"}))  # type: ignore[attr-defined]
    # One turn: read the user message, send back a single agent frame.
    raw = await websocket.recv()  # type: ignore[attr-defined]
    frame = json.loads(raw)
    reply = f"echo:{frame.get('text', '')}"
    await websocket.send(  # type: ignore[attr-defined]
        json.dumps({"type": "message", "from": "agent", "text": reply})
    )


@pytest.mark.asyncio
async def test_live_handshake_and_turn_roundtrip() -> None:
    from websockets.asyncio.server import serve

    async with serve(_chat_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = GatewayChatClient(f"http://127.0.0.1:{port}", "employee", "tok")
        await client.connect()
        try:
            events = [ev async for ev in client.send_turn("hi")]
        finally:
            await client.aclose()

    assert events == [TurnEvent("message", "echo:hi"), TurnEvent("done")]
