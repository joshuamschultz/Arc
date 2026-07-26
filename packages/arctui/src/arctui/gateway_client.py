"""GatewayChatClient — attach to a served agent over the gateway's WS chat route.

The gateway (``arc ui start`` / ``arc team serve``) owns the ``ArcAgent`` and
exposes ``/ws/chat/{agent_id}``; the browser dashboard and every platform
adapter reach the agent through it. This client speaks the same frame protocol
so the TUI is just one more viewpoint — no second ``ArcAgent``, no WORM
collision.

Protocol (matches ``arcui.routes.chat_ws`` + the web adapter):
  1. Connect, send ``{"token": <viewer-token>}``.
  2. Server replies ``{"type": "ready", "chat_id": ...}`` (or closes on bad auth).
  3. Per turn: send ``{"type": "message", "text": ...}``; the web adapter is
     send-only, so the reply arrives as one ``{"type": "message",
     "from": "agent", "text": ...}`` frame (block-at-turn).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from urllib.parse import quote

from arctui.transport import TurnEvent

# Injected connector: ``async (ws_url) -> connection`` where connection exposes
# awaitable ``send(str)`` / ``recv() -> str`` / ``close()``. Tests pass a double;
# production uses the ``websockets`` asyncio client.
Connector = Callable[[str], Awaitable[Any]]

# A turn can involve tool calls before the model replies; keep the ceiling well
# above a normal turn but bounded so a dead server never hangs the UI forever.
_DEFAULT_RECV_TIMEOUT = 300.0


class GatewayError(RuntimeError):
    """Base class for gateway-client failures."""


class GatewayAuthError(GatewayError):
    """The server rejected the viewer token (no ``ready`` handshake)."""


def _to_ws_url(base_url: str, agent_id: str) -> str:
    """Map an HTTP dashboard base URL to the agent's chat WebSocket URL."""
    base = base_url.rstrip("/")
    if base.startswith("https://"):
        scheme_rest = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        scheme_rest = "ws://" + base[len("http://") :]
    else:  # already a ws/wss URL
        scheme_rest = base
    return f"{scheme_rest}/ws/chat/{quote(agent_id, safe='')}"


async def _default_connect(ws_url: str) -> Any:
    """Open a real WebSocket via the ``websockets`` asyncio client."""
    from websockets.asyncio.client import connect

    return await connect(ws_url)


class GatewayChatClient:
    """WebSocket ``ChatTransport`` onto a served agent's ``/ws/chat`` route."""

    def __init__(
        self,
        base_url: str,
        agent_id: str,
        token: str,
        *,
        connect: Connector | None = None,
        recv_timeout: float = _DEFAULT_RECV_TIMEOUT,
    ) -> None:
        self._ws_url = _to_ws_url(base_url, agent_id)
        self._token = token
        self._connect = connect or _default_connect
        self._recv_timeout = recv_timeout
        self._ws: Any | None = None

    async def connect(self) -> None:
        """Open the socket and complete the token handshake (fail-loud)."""
        ws = await self._connect(self._ws_url)
        await ws.send(json.dumps({"token": self._token}))
        raw = await asyncio.wait_for(ws.recv(), self._recv_timeout)
        frame = json.loads(raw)
        if frame.get("type") != "ready":
            await ws.close()
            detail = frame.get("error") or frame.get("message") or frame
            raise GatewayAuthError(f"gateway rejected connection: {detail}")
        self._ws = ws

    async def send_turn(self, text: str) -> AsyncIterator[TurnEvent]:
        """Send *text* as a user turn; yield the agent reply, then ``done``."""
        if self._ws is None:
            raise RuntimeError("GatewayChatClient.send_turn called before connect()")

        await self._ws.send(json.dumps({"type": "message", "text": text}))
        while True:
            raw = await asyncio.wait_for(self._ws.recv(), self._recv_timeout)
            frame = json.loads(raw)
            ftype = frame.get("type")
            if ftype == "message" and frame.get("from") == "agent":
                yield TurnEvent("message", frame.get("text", ""))
                break
            if ftype == "error":
                detail = frame.get("message") or frame.get("error") or "gateway error"
                yield TurnEvent("error", str(detail))
                break
            # ready/ping/non-agent frames are not turn output — keep reading.
        yield TurnEvent("done")

    async def aclose(self) -> None:
        """Close the socket. Idempotent."""
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
