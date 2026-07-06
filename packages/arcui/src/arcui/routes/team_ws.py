"""Team stream WebSocket route — ``/ws/team`` (SPEC-031 F1/F2).

A read-only window onto the arcteam bus for the browser, plus a one-way
*forward* for human group posts. The route is a **thin view**:

* **Stream (F1):** it drains ``app.state.team_stream`` (a :class:`TeamStreamHub`
  fed by the read-only :class:`TeamBusObserver`) and pushes each rendered frame
  to the browser. Frames carry handles, never DIDs, and mark ``@mentions``.
* **Forward (F2):** a ``{"type": "post", "channel": ..., "text": ...}`` frame is
  handed to ``app.state.team_post_forwarder`` — the arcteam-owned callable that
  signs and routes it as the human entity. arcui derives the human's identity
  from the viewer token and passes it through; it never signs or routes.

Auth mirrors ``/ws/chat``: first-message token, ``viewer``/``operator`` only —
``agent`` tokens are rejected (ASI03). The route holds no messaging state.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from arcgateway.identity import derive_viewer_did
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from arcui.ws_helpers import CLOSE_AUTH_INVALID, authenticate_ws, run_ws_tasks

logger = logging.getLogger(__name__)


async def _drain_to_browser(ws: WebSocket, hub: Any) -> None:
    """Forward each queued team frame to the browser until the socket dies."""
    while True:
        frame = await hub.next_frame(ws)
        await ws.send_json(frame)


async def _receive_from_browser(ws: WebSocket, sender: str, forwarder: Any) -> None:
    """Forward human group posts to arcteam; never route or sign here."""
    async for raw in ws.iter_text():
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError as exc:
            await ws.send_json({"type": "error", "code": "malformed", "message": str(exc)})
            continue
        if not isinstance(frame, dict) or frame.get("type") != "post":
            continue  # non-post frames (pings, etc.) are ignored

        channel = frame.get("channel")
        text = frame.get("text", "")
        if not isinstance(channel, str) or not channel:
            await ws.send_json(
                {"type": "error", "code": "missing_channel", "message": "post requires a channel"}
            )
            continue
        if not isinstance(text, str) or not text:
            await ws.send_json({"type": "error", "code": "empty", "message": "post requires text"})
            continue
        if forwarder is None:
            await ws.send_json(
                {
                    "type": "error",
                    "code": "forward_unavailable",
                    "message": "team posting is not configured on this server",
                }
            )
            continue
        try:
            await forwarder(sender=sender, channel=channel, text=text)
        except Exception:  # reason: forwarding owner failed — inform, don't crash the view
            logger.exception("team_ws: forward to arcteam failed")
            await ws.send_json(
                {"type": "error", "code": "forward_failed", "message": "could not forward post"}
            )
            continue
        await ws.send_json({"type": "posted", "channel": channel})


async def team_ws_endpoint(ws: WebSocket) -> None:
    """Accept a browser WebSocket, stream team flows, forward group posts.

    Closes with 4003 on an invalid/agent token; 1000 on clean shutdown.
    """
    await ws.accept()

    role, msg = await authenticate_ws(ws, ws.app.state.auth_config)
    if role is None:
        return  # authenticate_ws already closed the socket
    if role not in ("viewer", "operator"):
        await ws.send_json({"error": "Agent tokens cannot open the team stream"})
        await ws.close(code=CLOSE_AUTH_INVALID)
        return

    hub = getattr(ws.app.state, "team_stream", None)
    if hub is None:
        await ws.send_json({"error": "Team stream is not enabled on this server"})
        await ws.close(code=CLOSE_AUTH_INVALID)
        return

    channel = ws.query_params.get("channel")
    scope = {channel} if channel else None
    hub.register(ws, channels=scope)

    # The human's identity is derived from the viewer token, never client-
    # supplied — the forwarder (arcteam) signs as this entity.
    sender = derive_viewer_did(msg.get("token", ""))
    forwarder = getattr(ws.app.state, "team_post_forwarder", None)

    await ws.send_json({"type": "ready"})

    try:
        done, _pending = await run_ws_tasks(
            _drain_to_browser(ws, hub),
            _receive_from_browser(ws, sender, forwarder),
        )
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                logger.debug("team_ws: task ended with %r", exc)
    except WebSocketDisconnect:
        pass
    finally:
        hub.unregister(ws)


routes = [
    WebSocketRoute("/ws/team", team_ws_endpoint),
]
