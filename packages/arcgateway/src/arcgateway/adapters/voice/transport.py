"""Voice transport — v1 push-to-talk over WebSocket (SPEC-077 COMP-007, REQ-008/009).

The spec's target transport is WebRTC (D-762) for echo-cancelled full-duplex
barge-in. v1 ships **push-to-talk over a WebSocket** instead (recorded deviation):
push-to-talk is half-duplex — the client never plays audio while recording — so
there is no echo to cancel and a plain WebSocket suffices. WebRTC + AEC + wake-word
barge-in are the follow-up.

Protocol (one utterance, one reply):
  client -> {"t":"auth","token": "<pairing token>"}   (first message)
  server -> {"t":"ready","chat_id": "<id>"}  |  {"t":"denied"} then close
  client -> <binary utterance: 16 kHz mono PCM-16>
  server -> {"t":"status","text": "..."}*  then  <binary reply: WAV bytes>

Auth is a pairing token in the handshake; mTLS / wss is the follow-up (D-743).
``websockets`` is imported lazily so the module stays import-cheap without [voice].
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

#: Utterance ceiling: ~30 s of 16 kHz mono PCM-16 is ~1 MB; 16 MB is generous.
MAX_UTTERANCE = 16 * 1024 * 1024

#: token -> (user_did, chat_id), or None to refuse the connection.
Authenticate = Callable[[str], "tuple[str, str] | None"]
#: called with (link, utterance_pcm) for each completed utterance.
OnUtterance = Callable[["VoiceLink", bytes], Awaitable[None]]


@dataclass
class VoiceLink:
    """One connected client. Outbound audio/status go back down this link."""

    chat_id: str
    user_did: str
    _ws: Any

    async def send_audio(self, wav: bytes) -> None:
        await self._ws.send(wav)

    async def say_status(self, text: str) -> None:
        await self._ws.send(json.dumps({"t": "status", "text": text}))


class VoiceServer:
    """Accepts client links, authenticates, and pumps utterances to a callback."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        authenticate: Authenticate,
        on_utterance: OnUtterance,
    ) -> None:
        self._host = host
        self._port = port
        self._authenticate = authenticate
        self._on_utterance = on_utterance
        self._server: Any = None

    async def start(self) -> None:
        from websockets.asyncio.server import serve  # lazy: optional [voice] dep

        self._server = await serve(
            self._handle, self._host, self._port, max_size=MAX_UTTERANCE
        )

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
        await websocket.send(json.dumps({"t": "ready", "chat_id": link.chat_id}))
        async for message in websocket:
            if isinstance(message, bytes):
                await self._on_utterance(link, message)
            # non-binary traffic after auth is ignored (status is server->client only)

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

    async def send_utterance(self, pcm: bytes) -> bytes | None:
        """Send one PTT utterance; return the reply WAV (skipping status texts)."""
        await self._ws.send(pcm)
        async for message in self._ws:
            if isinstance(message, bytes):
                return message
            # status text: caller could surface it; keep waiting for the audio
        return None

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None


__all__ = [
    "MAX_UTTERANCE",
    "Authenticate",
    "OnUtterance",
    "VoiceClient",
    "VoiceLink",
    "VoiceServer",
]
