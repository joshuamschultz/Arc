"""VoiceAdapter — lifecycle, translation, delivery, and no fourth thing (COMP-001).

The turn, wired: a client link sends an utterance (16 kHz mono PCM-16) → STT
(``engine.listen``) → a text Part → ``on_message`` (the gateway routes it to the
agent) → the agent's reply arrives via ``send`` → the output contract shortens it
for the ear → TTS (``engine.speak``) → the WAV goes back down the same link
(reply stays on its origin, REQ-002).

Download, naming, ceilings, audit fan-out, session identity, pairing custody and
splitting stay with the gateway (SPEC-065 REQ-310). The adapter emits per-turn
audit events at the single ``arctrust`` emission point via ``audit.emit_event``,
and holds no transcript/audio beyond the turn.
"""

from __future__ import annotations

from typing import Any

from arcgateway.adapters.base import DraftPart, InboundDraft, Outbound, as_parts
from arcgateway.adapters.registry import OnMessage
from arcgateway.adapters.voice.engine.base import FakeVoiceEngine, VoiceEngine
from arcgateway.adapters.voice.telemetry import voice_span
from arcgateway.adapters.voice.transport import Authenticate, VoiceLink, VoiceServer
from arcgateway.adapters.voice.ux.output_contract import OutputContract
from arcgateway.audit import emit_event
from arcgateway.delivery import DeliveryTarget
from arcgateway.parts import TextPart


class VoiceAdapter:
    """A voice session as a platform adapter, bound to one agent."""

    #: Outbound capability the gateway may use — voice carries spoken audio.
    supports: tuple[str, ...] = ("audio",)

    def __init__(
        self,
        *,
        on_message: OnMessage,
        agent_did: str,
        engine: VoiceEngine | None = None,
        host: str = "127.0.0.1",
        port: int = 8790,
        authenticate: Authenticate | None = None,
        tier: str = "personal",
    ) -> None:
        self.name = "voice"
        self.agent_did = agent_did
        self._on_message = on_message
        self.engine: VoiceEngine = engine or FakeVoiceEngine()
        self._tier = tier
        self._contract = OutputContract()
        self._authenticate: Authenticate = authenticate or (lambda _token: None)
        self._links: dict[str, VoiceLink] = {}
        self._server = VoiceServer(
            host=host, port=port, authenticate=self._authenticate, on_utterance=self._on_utterance
        )

    async def connect(self) -> None:
        await self._server.start()
        emit_event("voice.server.started", f"voice:{self.agent_did}", "allow", tier=self._tier)

    def bound_port(self) -> int:
        """The port the voice server is listening on (useful when started on 0)."""
        return self._server.bound_port()

    async def disconnect(self) -> None:
        await self._server.stop()
        await self.engine.aclose()
        self._links.clear()

    async def _on_utterance(self, link: VoiceLink, pcm: bytes) -> None:
        self._links[link.chat_id] = link
        emit_event(
            "voice.utterance.received",
            f"voice:{link.chat_id}",
            "allow",
            actor_did=link.user_did,
            tier=self._tier,
            extra={"bytes": len(pcm)},
        )
        with voice_span("voice.stt", chat_id=link.chat_id, bytes=len(pcm)):
            transcript = await self.engine.listen(pcm)
        parts = self.to_parts({"transcript": transcript})
        if not parts:
            return
        emit_event(
            "voice.transcribed",
            f"voice:{link.chat_id}",
            "allow",
            actor_did=link.user_did,
            tier=self._tier,
            extra={"chars": len(transcript)},  # length only — never the content (LLM02)
        )
        await self._on_message(
            InboundDraft(
                platform="voice",
                chat_id=link.chat_id,
                user_did=link.user_did,
                agent_did=self.agent_did,
                parts=parts,
            )
        )

    def to_parts(self, payload: Any) -> list[DraftPart]:
        transcript = ""
        if isinstance(payload, dict):
            transcript = str(payload.get("transcript") or "").strip()
        if not transcript:
            return []
        return [TextPart(text=transcript)]

    async def send(
        self,
        target: DeliveryTarget,
        parts: Outbound,
        *,
        reply_to: str | None = None,
    ) -> None:
        text = " ".join(p.text for p in as_parts(parts) if isinstance(p, TextPart)).strip()
        if not text:
            return
        spoken = self._contract.to_speech(text).speech
        link = self._links.get(target.chat_id)
        if link is None:
            emit_event("voice.reply.no_link", f"voice:{target.chat_id}", "warn", tier=self._tier)
            return
        with voice_span("voice.tts", chat_id=target.chat_id, words=len(spoken.split())):
            audio = await self.engine.speak(spoken)
        await link.send_audio(audio)
        emit_event(
            "voice.reply.spoken",
            f"voice:{target.chat_id}",
            "allow",
            actor_did=link.user_did,
            tier=self._tier,
            extra={"words": len(spoken.split())},
        )

    async def send_with_id(self, target: DeliveryTarget, message: str) -> str | None:
        await self.send(target, message)
        return None


__all__ = ["VoiceAdapter"]
