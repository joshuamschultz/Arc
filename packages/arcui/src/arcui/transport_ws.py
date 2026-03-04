"""WebSocketTransport — client-side UITransport over WebSocket.

Used by UIReporter module in arcagent to connect to the UI server.
Implements decorrelated jitter backoff for reconnection and a bounded
local buffer for events during disconnects.
"""

from __future__ import annotations

import json
import logging
import random
from collections import deque
from typing import Any

from arcui.types import ControlMessage, ControlResponse, UIEvent

logger = logging.getLogger(__name__)


def _decorrelated_jitter(base: float, cap: float, prev_sleep: float) -> float:
    """Decorrelated jitter backoff (AWS/Netflix standard).

    Returns a sleep duration in [base, min(cap, prev_sleep * 3)].
    Spreads reconnect storms better than full jitter.
    """
    upper = min(cap, prev_sleep * 3)
    return max(base, random.uniform(base, upper))  # noqa: S311 — not crypto


class WebSocketTransport:
    """WebSocket UITransport with reconnect and local buffering.

    This is the **client-side** transport used by agents. It connects to
    the UI server's ``/api/agent/connect`` endpoint, buffers events locally
    during disconnects, and flushes on reconnect.

    The actual WebSocket connection lifecycle (connect, reconnect loop,
    send/receive) is managed externally by UIReporterModule. This class
    provides the buffer, backoff calculation, and message framing.
    """

    def __init__(
        self,
        url: str,
        token: str,
        reconnect_base: float = 1.0,
        reconnect_cap: float = 60.0,
        buffer_size: int = 1000,
    ) -> None:
        self.url = url
        self.token = token
        self.reconnect_base = reconnect_base
        self.reconnect_cap = reconnect_cap
        self._max_buffer = buffer_size
        self._buffer: deque[tuple[str, UIEvent | ControlResponse]] = deque(
            maxlen=buffer_size
        )
        self._ws: Any | None = None
        self._closed = False
        self._last_sleep = reconnect_base

    @property
    def buffer_size(self) -> int:
        """Number of events currently buffered."""
        return len(self._buffer)

    @property
    def connected(self) -> bool:
        """Whether a WebSocket connection is active."""
        return self._ws is not None and not self._closed

    def buffer_event(
        self, agent_id: str, event: UIEvent | ControlResponse
    ) -> None:
        """Buffer an event locally. Drops oldest if buffer is full.

        Uses deque(maxlen=N) which automatically drops from the left
        (oldest) when appending beyond capacity.
        """
        self._buffer.append((agent_id, event))

    def drain_buffer(self) -> list[tuple[str, UIEvent | ControlResponse]]:
        """Remove and return all buffered events in order."""
        items = list(self._buffer)
        self._buffer.clear()
        return items

    def next_backoff(self) -> float:
        """Calculate next reconnect sleep using decorrelated jitter."""
        self._last_sleep = _decorrelated_jitter(
            self.reconnect_base, self.reconnect_cap, self._last_sleep
        )
        return self._last_sleep

    def reset_backoff(self) -> None:
        """Reset backoff after successful connection."""
        self._last_sleep = self.reconnect_base

    async def send_event(
        self, agent_id: str, event: UIEvent | ControlResponse
    ) -> None:
        """Send event over WebSocket, or buffer if disconnected."""
        if not self.connected:
            self.buffer_event(agent_id, event)
            return
        try:
            payload = {
                "agent_id": agent_id,
                "type": "event",
                "payload": event.model_dump(),
            }
            await self._ws.send(json.dumps(payload))
        except (ConnectionError, OSError, RuntimeError):
            logger.warning("Send failed, buffering event", exc_info=True)
            self.buffer_event(agent_id, event)

    async def send_control(
        self, agent_id: str, message: ControlMessage
    ) -> None:
        """Send control message (used by server side, not typical for client)."""
        if not self.connected:
            raise RuntimeError("Not connected")
        payload = {
            "agent_id": agent_id,
            "type": "control",
            "payload": message.model_dump(),
        }
        await self._ws.send(json.dumps(payload))

    async def receive(
        self,
    ) -> tuple[str, UIEvent | ControlMessage | ControlResponse]:
        """Receive next message from WebSocket."""
        if not self.connected:
            raise RuntimeError("Not connected")
        raw = await self._ws.recv()
        data = json.loads(raw)
        agent_id = data.get("agent_id", "")
        msg_type = data.get("type", "")
        payload = data.get("payload", {})

        if msg_type == "control":
            return agent_id, ControlMessage(**payload)
        if msg_type == "control_response":
            return agent_id, ControlResponse(**payload)
        return agent_id, UIEvent(**payload)

    async def close(self) -> None:
        """Close the transport and flush remaining buffer."""
        self._closed = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except (ConnectionError, OSError, RuntimeError):
                logger.debug("Error closing WebSocket", exc_info=True)
            self._ws = None
