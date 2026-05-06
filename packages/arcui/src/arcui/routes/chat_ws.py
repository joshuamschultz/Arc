"""Chat WebSocket route — ``/ws/chat/{agent_id}``.

Thin proxy between the browser and ``arcgateway.adapters.web.WebPlatformAdapter``.
The route owns the viewer token (NFR-5) — it validates the token, derives
``user_did`` and ``chat_id``, and hands the WebSocket to the adapter via
``register_socket``. The adapter is secret-free.

Auth model (SPEC-023 §FR-16):
    1. The WebSocket handshake is accepted.
    2. The client sends a first JSON message ``{"token": "<bearer>"}``.
    3. The token is validated against ``app.state.auth_config``. Only
       ``viewer`` and ``operator`` roles may upgrade; ``agent`` tokens
       are rejected (ASI03).
    4. The route derives:
         user_did = derive_viewer_did(token)            # arcgateway.identity
         chat_id  = build_session_key(agent_did, user_did)  # arcgateway.session
       The viewer token feeds ``derive_viewer_did`` only; ``chat_id``
       hashes the derived ``user_did``, not the raw token. The token
       never reaches the adapter or any audit/log/hash beyond that
       single derivation step.

``chat_id == session_key``: identical across web/slack/telegram for the
same (agent, user) pair. arcagent's SessionManager writes the session
JSONL under this same identifier (``<workspace>/sessions/<sid>.jsonl``),
so ``/api/agents/{id}/sessions/{chat_id}`` returns the conversation's
prior turns on reconnect.

After auth the loop is straightforward: each inbound JSON frame is
validated and forwarded to ``adapter.ingest``. The route never holds
state — duplicate browser tabs simply register two WebSockets for the
same chat_id and the adapter fans the agent's reply out to both.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from arcgateway.identity import derive_viewer_did
from arcgateway.session import build_session_key
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from arcui.ws_helpers import (
    CLOSE_AUTH_INVALID,
    authenticate_ws,
)

if TYPE_CHECKING:
    from arcgateway.adapters.web import WebPlatformAdapter

logger = logging.getLogger(__name__)

# Custom close codes — extends Starlette's catalog without colliding.
_CLOSE_AGENT_NOT_FOUND = 4404
_CLOSE_TOO_MANY_CONNECTIONS = 4429


_MAX_SINCE_SEQ = 2**31  # bounds an attacker-controlled int (SPEC-025 §M1)
_MAX_SINCE_SEQ_DIGITS = 12  # rejects multi-megabyte digit strings before int()


def _parse_since_seq(raw: str | None) -> int | None:
    """Parse the ``?since_seq`` query param.

    Treats any non-int or negative input as ``None`` (no replay). Used by
    reconnecting clients per SPEC-025 Track A — the value is the highest
    ``seq`` the client has already received, so the adapter replays anything
    strictly after it.

    Bounded: rejects strings longer than ``_MAX_SINCE_SEQ_DIGITS`` to defeat
    the slow-DoS where Python's arbitrary-precision int parsing is O(n²)
    on digit count, and caps the parsed value at ``_MAX_SINCE_SEQ``.
    """
    if raw is None:
        return None
    if len(raw) > _MAX_SINCE_SEQ_DIGITS:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value < 0 or value > _MAX_SINCE_SEQ:
        return None
    return value


def _resolve_agent_did(ws: WebSocket, agent_id: str) -> str | None:
    """Find the agent DID for a roster id, or None if unknown.

    ``team_roster.RosterEntry`` exposes ``agent_id``, ``name``, and ``did``.
    Match on either ``agent_id`` or ``name`` — both refer to the same agent
    in the SPEC-022 roster contract.
    """
    roster_provider = getattr(ws.app.state, "roster_provider", None)
    if roster_provider is None:
        return None
    for entry in roster_provider():
        if getattr(entry, "agent_id", None) == agent_id:
            return getattr(entry, "did", None) or ""
        if getattr(entry, "name", None) == agent_id:
            return getattr(entry, "did", None) or ""
    return None


async def chat_ws_endpoint(ws: WebSocket) -> None:
    """Accept a browser WebSocket and route messages through the gateway.

    Closes with one of:
      4001/4003 — auth timeout / invalid token (handled by ``authenticate_ws``)
      4404      — agent_id not in roster
      4429      — adapter at max_connections capacity
      1000      — clean shutdown / server-side close
    """
    await ws.accept()

    auth_config = ws.app.state.auth_config
    role, msg = await authenticate_ws(ws, auth_config)
    if role is None:
        return  # authenticate_ws already closed the WS

    if role not in ("viewer", "operator"):
        await ws.send_json({"error": "Agent tokens cannot open chat sessions"})
        await ws.close(code=CLOSE_AUTH_INVALID)
        return

    web_adapter: WebPlatformAdapter | None = getattr(ws.app.state, "web_adapter", None)
    if web_adapter is None:
        await ws.send_json({"error": "Chat is not enabled on this server"})
        await ws.close(code=CLOSE_AUTH_INVALID)
        return

    agent_id = ws.path_params["agent_id"]
    agent_did = _resolve_agent_did(ws, agent_id)
    if not agent_did:
        await ws.send_json({"error": f"Agent {agent_id!r} not found"})
        await ws.close(code=_CLOSE_AGENT_NOT_FOUND)
        return

    viewer_token: str = msg.get("token", "")
    user_did = derive_viewer_did(viewer_token)
    # chat_id == session_key: one conversation per (agent, user), same
    # whether reached via web/slack/telegram. Same identifier the
    # arcagent SessionManager writes under (workspace/sessions/<sid>.jsonl)
    # so /api/agents/{id}/sessions/{chat_id} returns this conversation's
    # history on reconnect.
    chat_id = build_session_key(agent_did, user_did)

    # Lazy import keeps the route module's import surface minimal and
    # ensures the test for "arcui does not import arcagent" remains true.
    from arcgateway.adapters.web import WebAdapterFull

    # SPEC-025 Track A — reconnecting clients pass ?since_seq=N so the
    # adapter replays any frames they missed during the disconnect. Parse
    # leniently: a non-int or negative value is treated as "no replay
    # requested" (no recovery banner, no replay), which is safe — fresh
    # connections get the next stream of frames as they arrive.
    since_seq = _parse_since_seq(ws.query_params.get("since_seq"))

    try:
        web_adapter.register_socket(
            ws, agent_did, user_did, chat_id, since_seq=since_seq
        )
    except WebAdapterFull:
        await ws.send_json({"error": "Server at capacity"})
        await ws.close(code=_CLOSE_TOO_MANY_CONNECTIONS)
        return

    # Acknowledge a successful upgrade so the client UI can mark the
    # connection live and start showing inbound frames immediately.
    await ws.send_json({"type": "ready", "chat_id": chat_id})

    try:
        async for raw in ws.iter_text():
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError as exc:
                await ws.send_json(
                    {
                        "type": "error",
                        "code": "malformed",
                        "message": f"invalid JSON: {exc}",
                    }
                )
                continue

            if not isinstance(frame, dict) or frame.get("type") != "message":
                # Silently ignore non-message frames (e.g. pings) for now.
                continue

            try:
                await web_adapter.ingest(
                    chat_id,
                    frame.get("text", ""),
                    client_seq=frame.get("client_seq"),
                )
            except ValueError as exc:
                await ws.send_json(
                    {"type": "error", "code": "malformed", "message": str(exc)}
                )
    except WebSocketDisconnect:
        pass
    finally:
        web_adapter.unregister_socket(ws)


routes = [
    WebSocketRoute("/ws/chat/{agent_id}", chat_ws_endpoint),
]
