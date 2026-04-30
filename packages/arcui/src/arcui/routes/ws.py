"""WebSocket route — /ws with first-message auth and subscription support.

Browser clients connect to /ws, send a first-message JSON auth payload,
and receive a streamed event feed. Auth is enforced before any events
are delivered (ASI03, ASI09 — no unauthenticated access to agent data).

Subscription protocols
----------------------
``{"type": "subscribe", agents, layers, teams}``
    Filters subsequent UIEvent broadcasts (server-side filter).

``{"type": "subscribe:agent", "agent_id": "..."}``
    Per-agent file-change subscription (SDD §4.8 / PLAN 3.1):

    1. Resolve agent root via ``app.state.roster_provider``.
    2. Increment :class:`arcgateway.fs_watcher.WatcherManager` refcount —
       starts the watcher on the 0→1 transition.
    3. Register with :class:`arcui.file_change_bridge.FileChangeBridge`.
    4. Replay the bridge's bounded ring so the client sees the latest known
       state for that agent immediately.

``{"type": "unsubscribe:agent", "agent_id": "..."}``
    Reverses each step above (decrement, unregister, no replay).

A WS disconnect drops every per-agent subscription owned by the queue, even
if the client did not unsubscribe explicitly — the connection's owner is the
bridge, not the message.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from arcui.subscription import Subscription
from arcui.ws_helpers import (
    MAX_WS_MESSAGE_SIZE,
    authenticate_ws,
    heartbeat_loop,
    run_ws_tasks,
)

if TYPE_CHECKING:
    from pathlib import Path

    from arcui.file_change_bridge import FileChangeBridge

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
    watcher_manager = getattr(ws.app.state, "watcher_manager", None)
    file_change_bridge: FileChangeBridge | None = getattr(
        ws.app.state, "file_change_bridge", None
    )

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
                except json.JSONDecodeError:
                    continue  # Ignore non-JSON messages (pong, etc)

                msg_type = data.get("type")
                if msg_type == "subscribe" and subscription_manager is not None:
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
                elif msg_type == "subscribe:agent":
                    await _handle_subscribe_agent(
                        ws=ws,
                        queue=queue,
                        agent_id=data.get("agent_id", ""),
                        watcher_manager=watcher_manager,
                        file_change_bridge=file_change_bridge,
                        audit=audit,
                    )
                elif msg_type == "unsubscribe:agent":
                    await _handle_unsubscribe_agent(
                        queue=queue,
                        agent_id=data.get("agent_id", ""),
                        watcher_manager=watcher_manager,
                        file_change_bridge=file_change_bridge,
                        audit=audit,
                    )
        except (WebSocketDisconnect, RuntimeError):
            pass

    try:
        await run_ws_tasks(_send_events(), heartbeat_loop(ws), _receive())
    finally:
        connection_manager.unregister(queue)
        if subscription_manager is not None:
            subscription_manager.remove_subscription(queue)
        # Drop every per-agent subscription this connection owned; releasing
        # the watcher refcount in lockstep prevents leaked watchers when a
        # client disconnects mid-flow.
        if file_change_bridge is not None:
            released = file_change_bridge.remove_all_for(queue)
            if watcher_manager is not None:
                for agent_id in released:
                    await watcher_manager.unsubscribe(agent_id)


def _resolve_agent_root(app_state: Any, agent_id: str) -> Path | None:
    """Look up an agent's filesystem root via the injected roster provider.

    Mirrors :func:`arcui.routes.agent_detail._agent_root` — kept local because
    the route module already owns the auth/lifecycle surface and shouldn't
    take a hard dependency on agent_detail just for one lookup.
    """
    from pathlib import Path as _Path

    provider = getattr(app_state, "roster_provider", None)
    if provider is None:
        return None
    for entry in provider():
        if entry.agent_id == agent_id:
            return _Path(entry.workspace_path)
    return None


async def _handle_subscribe_agent(
    *,
    ws: WebSocket,
    queue: asyncio.Queue[str],
    agent_id: str,
    watcher_manager: Any,
    file_change_bridge: FileChangeBridge | None,
    audit: Any,
) -> None:
    """Handle ``subscribe:agent`` — start watcher, register, replay."""
    if not agent_id or watcher_manager is None or file_change_bridge is None:
        await ws.send_json(
            {"type": "subscribe:error", "agent_id": agent_id, "error": "not_supported"}
        )
        return
    agent_root = _resolve_agent_root(ws.app.state, agent_id)
    if agent_root is None:
        await ws.send_json(
            {"type": "subscribe:error", "agent_id": agent_id, "error": "unknown_agent"}
        )
        if audit:
            audit.audit_event(
                "subscription.agent.unknown", {"agent_id": agent_id, "transport": "browser_ws"}
            )
        return
    await watcher_manager.subscribe(agent_id, agent_root)
    file_change_bridge.add_subscription(queue, agent_id)
    file_change_bridge.replay_for(queue, agent_id)
    if audit:
        audit.audit_event(
            "subscription.agent.added", {"agent_id": agent_id, "transport": "browser_ws"}
        )


async def _handle_unsubscribe_agent(
    *,
    queue: asyncio.Queue[str],
    agent_id: str,
    watcher_manager: Any,
    file_change_bridge: FileChangeBridge | None,
    audit: Any,
) -> None:
    """Handle ``unsubscribe:agent`` — drop registration, decrement refcount."""
    if not agent_id or watcher_manager is None or file_change_bridge is None:
        return
    file_change_bridge.remove_subscription(queue, agent_id)
    await watcher_manager.unsubscribe(agent_id)
    if audit:
        audit.audit_event(
            "subscription.agent.removed", {"agent_id": agent_id, "transport": "browser_ws"}
        )


routes = [
    WebSocketRoute("/ws", websocket_endpoint),
]
