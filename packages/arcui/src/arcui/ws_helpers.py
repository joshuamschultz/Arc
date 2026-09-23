"""Shared WebSocket helpers — first-message auth and a task runner.

Used by the ``/ws/chat`` and ``/ws/team`` routes.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

# --- Constants ---
AUTH_TIMEOUT_SECONDS = 5.0
MAX_WS_MESSAGE_SIZE = 1_048_576  # 1 MB — DoS prevention

# Close codes (WebSocket standard + custom)
CLOSE_NORMAL = 1000
CLOSE_AUTH_TIMEOUT = 4001
CLOSE_AUTH_INVALID = 4003
CLOSE_AUTH_UNAVAILABLE = 4013
CLOSE_CAPACITY_FULL = 4029


async def authenticate_ws(
    ws: Any,
    auth_config: Any,
) -> tuple[str | None, dict[str, Any]]:
    """First-message auth flow for WebSocket connections.

    Reads one JSON message containing ``{"token": "..."}`` within the
    auth timeout. Validates the token against auth_config.

    Args:
        ws: Starlette WebSocket instance.
        auth_config: AuthConfig with ``validate_token(token) -> role | None``.

    Returns:
        (role, full_message_dict) on success.
        (None, {}) on failure (error already sent, WS already closed).
    """
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=AUTH_TIMEOUT_SECONDS)
        msg = json.loads(raw)
        if not isinstance(msg, dict):
            raise ValueError("auth message must be an object")
        token = msg.get("token", "")
        if not isinstance(token, str) or not token:
            raise ValueError("auth token must be a string")
    except (TimeoutError, json.JSONDecodeError, KeyError, ValueError):
        await ws.send_json({"error": "Auth timeout or invalid message"})
        await ws.close(code=CLOSE_AUTH_TIMEOUT)
        return None, {}

    role = await revalidate_ws(ws, token, auth_config)

    if role is None:
        return None, {}

    return role, msg


async def revalidate_ws(ws: Any, token: str, auth_config: Any) -> str | None:
    """Recheck current account authority before WebSocket use."""
    role = auth_config.validate_token(token)
    if role is None:
        await ws.send_json({"error": "Invalid token"})
        await ws.close(code=CLOSE_AUTH_INVALID)
        return None
    if not isinstance(role, str):
        await ws.send_json({"error": "Invalid role"})
        await ws.close(code=CLOSE_AUTH_INVALID)
        return None
    session = auth_config.identify(token)
    if session is None:
        if getattr(ws.app.state, "hosted", False):
            await ws.send_json({"error": "A person must sign in"})
            await ws.close(code=CLOSE_AUTH_INVALID)
            return None
        return role
    factory = getattr(ws.app.state, "user_store_factory", None)
    if factory is None:
        await ws.send_json({"error": "Account authority is unavailable"})
        await ws.close(code=CLOSE_AUTH_UNAVAILABLE)
        return None
    try:
        user = await asyncio.to_thread(lambda: factory().get(session.email))
    except Exception:
        await ws.send_json({"error": "Account authority is unavailable"})
        await ws.close(code=CLOSE_AUTH_UNAVAILABLE)
        return None
    if user is None or user.disabled or user.did != session.did:
        auth_config.sessions.revoke(token)
        await ws.send_json({"error": "Session is no longer authorized"})
        await ws.close(code=CLOSE_AUTH_INVALID)
        return None
    current_role = "operator" if user.is_operator else "viewer"
    auth_config.sessions.set_role(token, current_role)
    return current_role


async def monitor_ws_authority(ws: Any, token: str, auth_config: Any) -> None:
    """Bound stale subscription access to one five-second account lease."""
    while True:
        await asyncio.sleep(5)
        if await revalidate_ws(ws, token, auth_config) is None:
            return


async def run_ws_tasks(*coros: Any) -> tuple[set[Any], set[Any]]:
    """Run multiple coroutines concurrently, cancel remaining on first completion.

    Returns (done, cancelled) task sets for caller inspection.
    """
    tasks = [asyncio.create_task(c) for c in coros]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    return done, pending
