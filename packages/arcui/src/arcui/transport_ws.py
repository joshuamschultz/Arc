"""WebSocketTransport — client-side UITransport over WebSocket.

Used by UIReporter module in arcagent to connect to the UI server.
Implements decorrelated jitter backoff for reconnection and a bounded
local buffer for events during disconnects.
"""

from __future__ import annotations

import asyncio
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

    Connects to the UI server's ``/api/agent/connect`` endpoint,
    authenticates with a first-message token + registration payload,
    buffers events locally during disconnects, and flushes on reconnect.
    """

    def __init__(
        self,
        url: str,
        token: str,
        reconnect_base: float = 1.0,
        reconnect_cap: float = 60.0,
        buffer_size: int = 1000,
        registration: dict[str, Any] | None = None,
        token_provider: Any | None = None,
    ) -> None:
        self.url = url
        self.token = token
        # Optional callable that returns the current token. Called before each
        # (re)connect attempt so a token rotated by an arcui restart is picked
        # up without restarting the agent. Falls back to the static token.
        self._token_provider = token_provider
        self.reconnect_base = reconnect_base
        self.reconnect_cap = reconnect_cap
        self._max_buffer = buffer_size
        self._buffer: deque[tuple[str, UIEvent | ControlResponse]] = deque(maxlen=buffer_size)
        self._ws: Any | None = None
        self._closed = False
        self._last_sleep = reconnect_base
        self._connect_task: asyncio.Task[None] | None = None
        self._registration = registration or {}

    def _current_token(self) -> str:
        """Resolve the token to send for the next auth handshake."""
        if self._token_provider is not None:
            try:
                fresh = self._token_provider()
                if fresh:
                    if fresh != self.token:
                        logger.info("Token refreshed from provider before reconnect")
                        self.token = fresh
            except Exception:
                logger.debug("token_provider raised; using cached token", exc_info=True)
        return self.token

    @property
    def buffer_size(self) -> int:
        """Number of events currently buffered."""
        return len(self._buffer)

    @property
    def connected(self) -> bool:
        """Whether a WebSocket connection is active."""
        return self._ws is not None and not self._closed

    def buffer_event(self, agent_id: str, event: UIEvent | ControlResponse) -> None:
        """Buffer an event locally. Drops oldest if buffer is full."""
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

    def start(self) -> None:
        """Start the background connect loop."""
        if self._connect_task is None or self._connect_task.done():
            self._connect_task = asyncio.get_running_loop().create_task(self._connect_loop())

    async def _connect_loop(self) -> None:
        """Connect to UI server with reconnect and backoff."""
        try:
            import websockets
        except ImportError:
            logger.warning("websockets not installed, transport disabled")
            return

        while not self._closed:
            try:
                async with websockets.connect(self.url) as ws:
                    self._ws = ws
                    self.reset_backoff()

                    # First-message auth + registration. Refresh the token
                    # from the provider (e.g., re-read ~/.arcagent/ui-token)
                    # so a rotation since last connect is picked up.
                    auth_msg = {
                        "token": self._current_token(),
                        "registration": self._registration,
                    }
                    await ws.send(json.dumps(auth_msg))

                    # Wait for auth response
                    resp_raw = await ws.recv()
                    resp = json.loads(resp_raw)
                    if resp.get("type") != "auth_ok":
                        logger.error("UI server auth failed: %s", resp.get("error", resp))
                        self._ws = None
                        await asyncio.sleep(self.next_backoff())
                        continue

                    logger.info(
                        "Connected to UI server (agent_id=%s)",
                        resp.get("agent_id", "?"),
                    )

                    # Drain buffered events
                    buffered = self.drain_buffer()
                    for agent_id, event in buffered:
                        try:
                            payload = {
                                "agent_id": agent_id,
                                "type": "event",
                                "payload": event.model_dump(),
                            }
                            await ws.send(json.dumps(payload))
                        except Exception:
                            logger.debug("Failed to drain buffered event")

                    # Keep alive — read server messages (pings, controls)
                    async for raw_msg in ws:
                        try:
                            data = json.loads(raw_msg)
                            if data.get("type") == "ping":
                                continue
                        except json.JSONDecodeError:
                            continue

            except (ConnectionError, OSError, TimeoutError) as exc:
                logger.debug("WebSocket disconnected: %s", exc)
                self._ws = None
            except Exception:
                logger.debug("WebSocket error", exc_info=True)
                self._ws = None

            if not self._closed:
                delay = self.next_backoff()
                logger.debug("Reconnecting in %.1fs", delay)
                await asyncio.sleep(delay)

    async def send_event(self, agent_id: str, event: UIEvent | ControlResponse) -> None:
        """Send event over WebSocket, or buffer if disconnected."""
        if not self.connected:
            self.buffer_event(agent_id, event)
            return
        # _ws is non-None when connected — extract to narrow type for mypy.
        ws = self._ws
        if ws is None:
            self.buffer_event(agent_id, event)
            return
        try:
            payload = {
                "agent_id": agent_id,
                "type": "event",
                "payload": event.model_dump(),
            }
            await ws.send(json.dumps(payload))
        except (ConnectionError, OSError, RuntimeError):
            logger.warning("Send failed, buffering event", exc_info=True)
            self.buffer_event(agent_id, event)

    async def send_control(self, agent_id: str, message: ControlMessage) -> None:
        """Send control message."""
        if not self.connected:
            raise RuntimeError("Not connected")
        # _ws is non-None when connected — extract to narrow type for mypy.
        ws = self._ws
        if ws is None:
            raise RuntimeError("Not connected")
        payload = {
            "agent_id": agent_id,
            "type": "control",
            "payload": message.model_dump(),
        }
        await ws.send(json.dumps(payload))

    async def receive(self) -> tuple[str, ControlMessage]:
        """Receive a control message from the UI server.

        Reads one frame from the WebSocket, parses it as a ControlMessage,
        and returns ``(agent_id, control_message)``.

        Raises:
            RuntimeError: If not connected.
            ValueError: If the message is not a valid control frame.
        """
        if not self.connected:
            raise RuntimeError("Not connected")
        # _ws is non-None when connected — extract to narrow type for mypy.
        ws = self._ws
        if ws is None:
            raise RuntimeError("Not connected")
        raw = await ws.recv()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed control frame: {raw!r}") from exc

        if data.get("type") != "control":
            raise ValueError(f"Expected type='control', got {data.get('type')!r}")

        agent_id: str = data.get("agent_id", "")
        payload = data.get("payload", {})
        try:
            msg = ControlMessage(**payload)
        except Exception as exc:
            raise ValueError(f"Invalid ControlMessage payload: {payload}") from exc
        return agent_id, msg

    async def close(self) -> None:
        """Close the transport."""
        self._closed = True
        if self._connect_task is not None:
            self._connect_task.cancel()
            try:
                await self._connect_task
            except (asyncio.CancelledError, Exception):  # noqa: S110 — cancel cleanup, not user data
                pass
        if self._ws is not None:
            try:
                await self._ws.close()
            except (ConnectionError, OSError, RuntimeError):
                logger.debug("Error closing WebSocket", exc_info=True)
            self._ws = None
