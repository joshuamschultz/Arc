"""WebSocket route — /ws with first-message auth."""

from __future__ import annotations

import asyncio
import json
import logging

from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

_AUTH_TIMEOUT_SECONDS = 5.0
_HEARTBEAT_INTERVAL_SECONDS = 30.0


async def websocket_endpoint(ws: WebSocket) -> None:
    """WebSocket endpoint with first-message auth and event streaming."""
    await ws.accept()

    auth_config = ws.app.state.auth_config
    connection_manager = ws.app.state.connection_manager

    # First-message auth: expect {"token": "..."} within 5 seconds
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=_AUTH_TIMEOUT_SECONDS)
        msg = json.loads(raw)
        token = msg.get("token", "")
    except (TimeoutError, json.JSONDecodeError, KeyError):
        await ws.send_json({"error": "Auth timeout or invalid message"})
        await ws.close(code=4001)
        return

    role = auth_config.validate_token(token)
    if role is None:
        await ws.send_json({"error": "Invalid token"})
        await ws.close(code=4003)
        return

    await ws.send_json({"type": "auth_ok", "role": role})

    # Register client queue
    queue = connection_manager.create_queue()

    async def _send_events() -> None:
        """Stream events from queue to WebSocket."""
        try:
            while True:
                message = await queue.get()
                await ws.send_text(message)
        except (WebSocketDisconnect, RuntimeError):
            pass

    async def _heartbeat() -> None:
        """Send periodic ping to keep connection alive."""
        try:
            while True:
                await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)
                await ws.send_json({"type": "ping"})
        except (WebSocketDisconnect, RuntimeError):
            pass

    async def _receive() -> None:
        """Receive messages from client (pong, etc)."""
        try:
            while True:
                await ws.receive_text()  # Consume client messages
        except (WebSocketDisconnect, RuntimeError):
            pass

    try:
        # Run send, heartbeat, and receive concurrently
        _done, pending = await asyncio.wait(
            [
                asyncio.create_task(_send_events()),
                asyncio.create_task(_heartbeat()),
                asyncio.create_task(_receive()),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
    finally:
        connection_manager.unregister(queue)


routes = [
    WebSocketRoute("/ws", websocket_endpoint),
]
