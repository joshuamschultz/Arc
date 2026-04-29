"""Shared WebSocket helpers — auth, heartbeat, task runner, queue helpers.

Extracts common patterns from agent_ws.py and ws.py to reduce duplication.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# --- Constants ---
AUTH_TIMEOUT_SECONDS = 5.0
HEARTBEAT_INTERVAL_SECONDS = 30.0
MAX_WS_MESSAGE_SIZE = 1_048_576  # 1 MB — DoS prevention

# Close codes (WebSocket standard + custom)
CLOSE_NORMAL = 1000
CLOSE_AUTH_TIMEOUT = 4001
CLOSE_AUTH_INVALID = 4003
CLOSE_CAPACITY_FULL = 4029


async def authenticate_ws(
    ws: Any,
    auth_config: Any,
    *,
    require_role: str | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """First-message auth flow for WebSocket connections.

    Reads one JSON message containing ``{"token": "..."}`` within the
    auth timeout. Validates the token against auth_config.

    Args:
        ws: Starlette WebSocket instance.
        auth_config: AuthConfig with ``validate_token(token) -> role | None``.
        require_role: If set, rejects tokens that don't map to this role.

    Returns:
        (role, full_message_dict) on success.
        (None, {}) on failure (error already sent, WS already closed).
    """
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=AUTH_TIMEOUT_SECONDS)
        msg = json.loads(raw)
        token = msg.get("token", "")
    except (TimeoutError, json.JSONDecodeError, KeyError):
        await ws.send_json({"error": "Auth timeout or invalid message"})
        await ws.close(code=CLOSE_AUTH_TIMEOUT)
        return None, {}

    role = auth_config.validate_token(token)

    if require_role is not None:
        if role != require_role:
            await ws.send_json({"error": f"Invalid {require_role} token"})
            await ws.close(code=CLOSE_AUTH_INVALID)
            return None, {}
    elif role is None:
        await ws.send_json({"error": "Invalid token"})
        await ws.close(code=CLOSE_AUTH_INVALID)
        return None, {}

    return role, msg


async def heartbeat_loop(ws: Any) -> None:
    """Periodic ping keepalive for WebSocket connections."""
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            await ws.send_json({"type": "ping"})
    except Exception:  # noqa: S110 — WS lifecycle ends silently
        pass


async def run_ws_tasks(*coros: Any) -> tuple[set[Any], set[Any]]:
    """Run multiple coroutines concurrently, cancel remaining on first completion.

    Returns (done, cancelled) task sets for caller inspection.
    """
    tasks = [asyncio.create_task(c) for c in coros]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    return done, pending


def safe_enqueue(queue: asyncio.Queue[str], message: str) -> None:
    """Enqueue a message, dropping the oldest if the queue is full."""
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    try:
        queue.put_nowait(message)
    except asyncio.QueueFull:
        pass
