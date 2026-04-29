"""WebSocket route — /ws with first-message auth and subscription support.

Browser clients connect to /ws, send a first-message JSON auth payload,
and receive a streamed event feed. Auth is enforced before any events
are delivered (ASI03, ASI09 — no unauthenticated access to agent data).
"""

from __future__ import annotations

import json
import logging

from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from arcui.subscription import Subscription
from arcui.ws_helpers import (
    MAX_WS_MESSAGE_SIZE,
    authenticate_ws,
    heartbeat_loop,
    run_ws_tasks,
)

logger = logging.getLogger(__name__)


async def websocket_endpoint(ws: WebSocket) -> None:
    """WebSocket endpoint with first-message auth and event streaming.

    Auth flow:
      1. Accept the connection.
      2. Wait for a JSON first-message: {"token": "<bearer>"}.
      3. Validate the token via AuthConfig.
         - Invalid/missing → send {"error": "Invalid token"}, close 4003.
         - Agent token → rejected (agent tokens are for /api/agent/connect only).
         - Valid viewer/operator → send {"type": "auth_ok", "role": "<role>"}.
      4. Register the client queue and begin event streaming.
    """
    await ws.accept()

    auth_config = ws.app.state.auth_config
    connection_manager = ws.app.state.connection_manager
    subscription_manager = getattr(ws.app.state, "subscription_manager", None)
    audit = getattr(ws.app.state, "audit", None)

    # First-message auth — viewer and operator tokens accepted; agent tokens rejected.
    role, _msg = await authenticate_ws(ws, auth_config)
    if role is None:
        # authenticate_ws already sent the error and closed the connection.
        if audit:
            audit.audit_event("auth.failure", {"transport": "browser_ws"})
        return

    # Agent tokens must not access the browser WS feed (ASI03).
    if role == "agent":
        await ws.send_json({"error": "Agent tokens cannot access browser WebSocket"})
        from arcui.ws_helpers import CLOSE_AUTH_INVALID

        await ws.close(code=CLOSE_AUTH_INVALID)
        if audit:
            audit.audit_event(
                "auth.rejected", {"transport": "browser_ws", "reason": "agent_token"}
            )
        return

    await ws.send_json({"type": "auth_ok", "role": role})

    if audit:
        audit.audit_event("auth.success", {"transport": "browser_ws", "role": role})

    # Register client queue
    queue = connection_manager.create_queue()

    # Register with SubscriptionManager (default: receive all)
    if subscription_manager is not None:
        subscription_manager.set_subscription(queue, Subscription())

    async def _send_events() -> None:
        """Stream events from queue to WebSocket."""
        try:
            while True:
                message = await queue.get()
                await ws.send_text(message)
        except (WebSocketDisconnect, RuntimeError):
            pass

    async def _receive() -> None:
        """Receive messages from client (pong, subscribe, etc)."""
        try:
            while True:
                raw_msg = await ws.receive_text()

                # DoS prevention: reject oversized messages
                if len(raw_msg) > MAX_WS_MESSAGE_SIZE:
                    logger.warning("Oversized browser message (%d bytes), skipping", len(raw_msg))
                    continue

                try:
                    data = json.loads(raw_msg)
                    if data.get("type") == "subscribe" and subscription_manager is not None:
                        sub = Subscription(
                            agents=data.get("agents"),
                            layers=data.get("layers"),
                            teams=data.get("teams"),
                        )
                        subscription_manager.set_subscription(queue, sub)
                        logger.info("Browser updated subscription: %s", sub)
                        if audit:
                            audit.audit_event(
                                "subscription.update",
                                {"agents": data.get("agents"), "layers": data.get("layers")},
                            )
                except json.JSONDecodeError:
                    pass  # Ignore non-JSON messages (pong, etc)
        except (WebSocketDisconnect, RuntimeError):
            pass

    try:
        await run_ws_tasks(_send_events(), heartbeat_loop(ws), _receive())
    finally:
        connection_manager.unregister(queue)
        if subscription_manager is not None:
            subscription_manager.remove_subscription(queue)


routes = [
    WebSocketRoute("/ws", websocket_endpoint),
]
