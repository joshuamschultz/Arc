"""Voice transport — v1 push-to-talk over WebSocket (SPEC-077 COMP-007, REQ-008/009).

The spec's target transport is WebRTC (D-762) for echo-cancelled full-duplex
barge-in. v1 ships **push-to-talk over a WebSocket** instead (recorded deviation):
push-to-talk is half-duplex — the client never plays audio while recording — so
there is no echo to cancel and a plain WebSocket suffices. WebRTC + AEC + wake-word
barge-in are the follow-up.

Protocol (one utterance, one reply):
  client -> {"t":"auth","token": "<pairing token>"}   (first message)
  server -> {"t":"ready","chat_id": "<id>"}  |  {"t":"denied"} then close
  client -> {"t":"hello","mode","mic","wake","wake_loaded"}   (optional, after ready)
  client -> {"t":"state","state","last_wake_at","heard"?}      (heartbeat, ~5 s)
  server -> {"t":"cmd","listening"?, "wake"?, "test"?}         (operator control)
  client -> <binary utterance: 16 kHz mono PCM-16>
  server -> {"t":"status","text": "..."}*  then  <binary reply: WAV bytes>

``ready`` also carries ``listening`` and ``wake`` so the mic box needs no env edits.
Control flows server -> client only: nothing a client sends can change what wakes
the agent or whether it listens.

Auth is a pairing token in the handshake; mTLS / wss is the follow-up (D-743).
``websockets`` is imported lazily so the module stays import-cheap without [voice].
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from arcgateway.adapters.voice.status import CLIENT_MODES, CLIENT_STATES

#: Utterance ceiling: ~30 s of 16 kHz mono PCM-16 is ~1 MB; 16 MB is generous.
MAX_UTTERANCE = 16 * 1024 * 1024

_log = logging.getLogger("arcgateway.voice.transport")

#: Control frames are tiny; anything bigger is junk and is ignored.
MAX_CONTROL_BYTES = 2048
#: How long an operator "say it now" test may echo transcripts back to the card.
TEST_SECONDS = 30.0

#: token -> (user_did, chat_id), or None to refuse the connection.
Authenticate = Callable[[str], "tuple[str, str] | None"]
#: called with (link, utterance_pcm) for each completed utterance.
OnUtterance = Callable[["VoiceLink", bytes], Awaitable[None]]


@dataclass
class LinkRecord:
    """What an authenticated client last told us about itself (bounded, typed)."""

    connected_at: float = 0.0
    last_seen: float = 0.0
    mode: str = ""
    mic: str = ""
    wake: str = ""
    wake_loaded: bool = False
    state: str = "idle"
    last_wake_at: float | None = None


@dataclass
class VoiceLink:
    """One connected client. Outbound audio/status go back down this link."""

    chat_id: str
    user_did: str
    _ws: Any
    record: LinkRecord = field(default_factory=LinkRecord)

    async def send_audio(self, wav: bytes) -> None:
        await self._ws.send(wav)

    async def say_status(self, text: str) -> None:
        await self._ws.send(json.dumps({"t": "status", "text": text}))

    async def send_cmd(self, payload: dict[str, Any]) -> None:
        await self._ws.send(json.dumps({"t": "cmd", **payload}))


class VoiceServer:
    """Accepts client links, authenticates, and pumps utterances to a callback."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        authenticate: Authenticate,
        on_utterance: OnUtterance,
        ready_extra: Callable[[], dict[str, Any]] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ready_extra = ready_extra or (lambda: {})
        self._clock = clock
        self._live: dict[int, VoiceLink] = {}
        #: transcript echo for an operator test; text is kept only until it expires.
        self.test_until: float = 0.0
        self.test_heard: str = ""
        self._host = host
        self._port = port
        self._authenticate = authenticate
        self._on_utterance = on_utterance
        self._server: Any = None

    async def start(self) -> None:
        from websockets.asyncio.server import serve  # lazy: optional [voice] dep

        self._server = await serve(self._handle, self._host, self._port, max_size=MAX_UTTERANCE)

    def bound_port(self) -> int:
        """The actually-bound port (useful when started on port 0)."""
        return int(self._server.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, websocket: Any) -> None:
        ident = await self._authenticate_link(websocket)
        if ident is None:
            return
        link = VoiceLink(chat_id=ident[1], user_did=ident[0], _ws=websocket)
        link.record.connected_at = link.record.last_seen = self._clock()
        await websocket.send(
            json.dumps({"t": "ready", "chat_id": link.chat_id, **self._ready_extra()})
        )
        self._live[id(link)] = link
        try:
            async for message in websocket:
                if isinstance(message, bytes):
                    link.record.last_seen = self._clock()
                    await self._on_utterance(link, message)
                else:
                    self._on_control(link, message)
        finally:
            self._live.pop(id(link), None)

    def links(self) -> list[VoiceLink]:
        """Authenticated, currently connected links (never an unauthenticated one)."""
        return list(self._live.values())

    async def broadcast(self, payload: dict[str, Any]) -> int:
        """Send a ``cmd`` to every authenticated link; returns how many got it."""
        sent = 0
        for link in self.links():
            try:
                await link.send_cmd(payload)
                sent += 1
            except Exception:  # reason: one dead socket must not block the rest
                _log.debug("voice cmd not delivered to one link", exc_info=True)
        return sent

    def start_test(self) -> None:
        self.test_until = self._clock() + TEST_SECONDS
        self.test_heard = ""

    def test_active(self) -> bool:
        return self._clock() < self.test_until

    def _on_control(self, link: VoiceLink, raw: str) -> None:
        """Fold a client heartbeat into the link record. Bounded, typed, never trusted."""
        if len(raw) > MAX_CONTROL_BYTES:
            return
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        rec = link.record
        rec.last_seen = self._clock()
        kind = msg.get("t")
        if kind == "hello":
            mode = msg.get("mode")
            rec.mode = mode if mode in CLIENT_MODES else ""
            rec.mic = str(msg.get("mic", ""))[:64]
            rec.wake = str(msg.get("wake", ""))[:64]
            rec.wake_loaded = msg.get("wake_loaded") is True
        elif kind == "state":
            state = msg.get("state")
            rec.state = state if state in CLIENT_STATES else rec.state
            last = msg.get("last_wake_at")
            if isinstance(last, (int, float)) and not isinstance(last, bool):
                rec.last_wake_at = float(last)
            heard = msg.get("heard")
            if isinstance(heard, str) and self.test_active():
                self.test_heard = heard[:200]
        # Any other message type is ignored: wake words and listening are server-owned.

    async def _authenticate_link(self, websocket: Any) -> tuple[str, str] | None:
        try:
            raw = await websocket.recv()
            msg = json.loads(raw) if isinstance(raw, str) else {}
        except (ValueError, TypeError):
            msg = {}
        if not isinstance(msg, dict) or msg.get("t") != "auth":
            await websocket.send(json.dumps({"t": "denied"}))
            return None
        ident = self._authenticate(str(msg.get("token", "")))
        if ident is None:
            await websocket.send(json.dumps({"t": "denied"}))
            return None
        return ident


class VoiceClient:
    """Thin client end: authenticate, then send one utterance and get one reply."""

    def __init__(self, *, uri: str, token: str) -> None:
        self._uri = uri
        self._token = token
        self._ws: Any = None
        self.chat_id: str | None = None
        #: called with each server control payload (the ``ready`` and ``cmd`` frames).
        self.control_handler: Callable[[dict[str, Any]], None] = lambda _payload: None
        self._audio: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._pump_task: asyncio.Task[None] | None = None

    async def connect(self) -> None:
        from websockets.asyncio.client import connect  # lazy: optional [voice] dep

        self._ws = await connect(self._uri, max_size=MAX_UTTERANCE)
        await self._ws.send(json.dumps({"t": "auth", "token": self._token}))
        raw = await self._ws.recv()
        reply = json.loads(raw) if isinstance(raw, str) else {}
        if reply.get("t") != "ready":
            await self.close()
            raise PermissionError("voice pairing token was refused")
        self.chat_id = str(reply.get("chat_id") or "")
        self.control_handler(reply)
        self._pump_task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        """Read the socket continuously so control frames land while idle."""
        try:
            async for message in self._ws:
                if isinstance(message, bytes):
                    await self._audio.put(message)
                    continue
                try:
                    payload = json.loads(message)
                except ValueError:
                    continue
                if isinstance(payload, dict) and payload.get("t") == "cmd":
                    self.control_handler(payload)
        except Exception:  # reason: a dropped link ends the pump; callers see None
            _log.debug("voice client pump ended", exc_info=True)
        finally:
            await self._audio.put(None)

    async def send_json(self, payload: dict[str, Any]) -> None:
        """Send one control frame (hello/state) to the gateway."""
        if self._ws is not None:
            await self._ws.send(json.dumps(payload))

    async def send_utterance(self, pcm: bytes) -> bytes | None:
        """Send one utterance; return the reply WAV (status texts are skipped)."""
        await self._ws.send(pcm)
        return await self._audio.get()

    async def close(self) -> None:
        if self._pump_task is not None:
            self._pump_task.cancel()
            self._pump_task = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None


__all__ = [
    "MAX_CONTROL_BYTES",
    "MAX_UTTERANCE",
    "TEST_SECONDS",
    "Authenticate",
    "LinkRecord",
    "OnUtterance",
    "VoiceClient",
    "VoiceLink",
    "VoiceServer",
]
