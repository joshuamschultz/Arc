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

import time
from typing import Any

from arcgateway.adapters.base import DraftPart, InboundDraft, Outbound, as_parts
from arcgateway.adapters.registry import OnMessage
from arcgateway.adapters.voice.config import WakeConfig
from arcgateway.adapters.voice.engine.base import FakeVoiceEngine, VoiceEngine
from arcgateway.adapters.voice.status import (
    STALE_AFTER_SECONDS,
    EngineStatus,
    PartStatus,
    VoiceLiveStatus,
)
from arcgateway.adapters.voice.telemetry import voice_span
from arcgateway.adapters.voice.transport import (
    Authenticate,
    LinkRecord,
    VoiceLink,
    VoiceServer,
)
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
        chat_id: str = "voice",
        listening: bool = True,
        wake: WakeConfig | None = None,
        engine_names: tuple[str | None, str | None] = (None, None),
    ) -> None:
        self.name = "voice"
        self._chat_id = chat_id
        self._listening = listening
        self._wake = wake or WakeConfig()
        self._engine_names = engine_names
        self._started_at: float | None = None
        self._engine_used_at: float | None = None
        self._engine_error = ""
        self.agent_did = agent_did
        self._on_message = on_message
        self.engine: VoiceEngine = engine or FakeVoiceEngine()
        self._tier = tier
        self._contract = OutputContract()
        self._authenticate: Authenticate = authenticate or (lambda _token: None)
        self._links: dict[str, VoiceLink] = {}
        self._server = VoiceServer(
            host=host,
            port=port,
            authenticate=self._authenticate,
            on_utterance=self._on_utterance,
            ready_extra=self._control_payload,
        )

    def _wake_payload(self) -> dict[str, Any]:
        words = list(self._wake.words) or [self._chat_id.lower()]
        return {
            "words": words,
            "mode": self._wake.mode,
            "match": self._wake.match,
            "model_path": self._wake.model_path,
        }

    def _control_payload(self) -> dict[str, Any]:
        return {"listening": self._listening, "wake": self._wake_payload()}

    @property
    def listening(self) -> bool:
        return self._listening

    async def set_listening(self, on: bool, *, actor_did: str) -> int:
        """Turn listening on/off live; returns how many mic clients were told.

        Takes effect without a restart: the flag gates ``_on_utterance`` here and
        the client stops capturing when it gets the command.
        """
        self._listening = on
        emit_event(
            "voice.listening.set",
            f"voice:{self.agent_did}",
            "allow",
            actor_did=actor_did,
            tier=self._tier,
            extra={"listening": on},
        )
        return await self._server.broadcast({"listening": on})

    async def set_wake(self, wake: WakeConfig, *, actor_did: str) -> int:
        """Replace the wake config live (already validated by ``WakeConfig``)."""
        old = self._wake_payload()
        self._wake = wake
        emit_event(
            "voice.wake.set",
            f"voice:{self.agent_did}",
            "allow",
            actor_did=actor_did,
            tier=self._tier,
            extra={"old": old, "new": self._wake_payload()},
        )
        return await self._server.broadcast({"wake": self._wake_payload()})

    async def start_test(self, *, actor_did: str) -> int:
        """Open a 30 s window in which the mic box echoes what it heard to the card."""
        self._server.start_test()
        emit_event(
            "voice.test.started",
            f"voice:{self.agent_did}",
            "allow",
            actor_did=actor_did,
            tier=self._tier,
        )
        return await self._server.broadcast({"test": True})

    def status(self, *, now: float | None = None) -> VoiceLiveStatus:
        """Live state of the three parts. Lengths and states only; no transcripts."""
        moment = time.time() if now is None else now
        wake = self._wake_payload()
        best = self._freshest_link(moment)
        client = self._client_part(best)
        engine = self._engine_part()
        state = self._display_state(best)
        return VoiceLiveStatus(
            listening=self._listening,
            state=state,
            client_connected=best is not None,
            reason=self._down_reason(client, engine),
            mode=best.mode if best else "",
            mic=best.mic if best else "",
            wake_words=wake["words"],
            wake_mode=self._wake.mode,
            wake_match=self._wake.match,
            wake_loaded=bool(best and best.wake_loaded),
            last_wake_at=best.last_wake_at if best else None,
            last_seen=best.last_seen if best else None,
            adapter=PartStatus(up=self._started_at is not None, heartbeat_at=moment),
            client=client,
            engine=engine,
            test_heard=self._server.test_heard if self._server.test_active() else None,
        )

    def _freshest_link(self, now: float) -> LinkRecord | None:
        fresh = [
            link.record
            for link in self._server.links()
            if now - link.record.last_seen <= STALE_AFTER_SECONDS
        ]
        return max(fresh, key=lambda rec: rec.last_seen, default=None)

    def _client_part(self, best: LinkRecord | None) -> PartStatus:
        if best is not None:
            return PartStatus(up=True, heartbeat_at=best.last_seen)
        if self._server.links():
            return PartStatus(
                up=False,
                reason=(
                    "The mic computer stopped reporting. "
                    "Check that the Arc mic app is still running."
                ),
            )
        return PartStatus(
            up=False,
            reason=(
                "No mic app is connected. "
                "Start the Arc mic app on the computer with the microphone."
            ),
        )

    def _engine_part(self) -> EngineStatus:
        stt, tts = self._engine_names
        base: dict[str, Any] = {
            "stt": stt,
            "tts": tts,
            "heartbeat_at": self._engine_used_at,
            "last_error": self._engine_error,
        }
        if stt is None or tts is None:
            return EngineStatus(
                up=False,
                reason="No speech engine is configured. Run connect-voice, then restart.",
                **base,
            )
        if self._engine_error:
            return EngineStatus(
                up=False, reason=f"Speech engine error: {self._engine_error}", **base
            )
        return EngineStatus(up=True, **base)

    def _display_state(self, best: LinkRecord | None) -> str:
        if best is None:
            return "offline"
        if not self._listening or best.state == "paused":
            return "paused"
        return "listening" if best.state == "idle" else best.state

    def _down_reason(self, client: PartStatus, engine: EngineStatus) -> str:
        if self._started_at is None:
            return "The voice adapter is not running. Restart the gateway."
        if not client.up:
            return client.reason
        if not engine.up:
            return engine.reason
        return ""

    async def connect(self) -> None:
        await self._server.start()
        self._started_at = time.time()
        emit_event("voice.server.started", f"voice:{self.agent_did}", "allow", tier=self._tier)

    def bound_port(self) -> int:
        """The port the voice server is listening on (useful when started on 0)."""
        return self._server.bound_port()

    async def disconnect(self) -> None:
        self._started_at = None
        await self._server.stop()
        await self.engine.aclose()
        self._links.clear()

    async def _on_utterance(self, link: VoiceLink, pcm: bytes) -> None:
        if not self._listening:
            # Paused: the client should not send, but a stale or hostile one might.
            emit_event(
                "voice.utterance.dropped_paused",
                f"voice:{link.chat_id}",
                "deny",
                actor_did=link.user_did,
                tier=self._tier,
                extra={"bytes": len(pcm)},
            )
            return
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
            transcript = await self._guarded(self.engine.listen(pcm))
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

    async def _guarded(self, call: Any) -> Any:
        """Await an engine call, recording its heartbeat or its plain failure reason."""
        try:
            result = await call
        except Exception as exc:
            self._engine_error = type(exc).__name__
            raise
        self._engine_error = ""
        self._engine_used_at = time.time()
        return result

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
            audio = await self._guarded(self.engine.speak(spoken))
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
