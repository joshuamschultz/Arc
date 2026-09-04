"""VoiceAdapter — the three adapter responsibilities, and no fourth (SPEC-077 COMP-001).

Lifecycle (``connect``/``disconnect``), translation (``to_parts``: a finished
utterance -> TextParts) and delivery (``send``: voice the reply through the
engine). Download, naming, ceilings, audit, session identity, pairing and
splitting stay with the gateway (SPEC-065 REQ-310) — this class must not touch
them, and the adapter contract-surface suite enforces that.

Phase 1 is the skeleton: ``connect`` does not yet open the WebRTC session or wire
a real engine (that is Phase 2, T-018/T-019). What is real here is the shape —
utterance in, spoken reply out, reply pinned to its own voice origin (REQ-002).
"""

from __future__ import annotations

from typing import Any

from arcgateway.adapters.base import DraftPart, Outbound, as_parts
from arcgateway.adapters.registry import OnMessage
from arcgateway.adapters.voice.engine.base import FakeVoiceEngine, VoiceEngine
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
    ) -> None:
        self.name = "voice"
        self.agent_did = agent_did
        self._on_message = on_message
        #: Default to the Fake in the skeleton; Phase 2 injects the cascade.
        self.engine: VoiceEngine = engine or FakeVoiceEngine()

    async def connect(self) -> None:
        # Phase 1 skeleton — the WebRTC session + cascade wiring lands in Phase 2.
        return None

    async def disconnect(self) -> None:
        await self.engine.aclose()

    def to_parts(self, payload: Any) -> list[DraftPart]:
        """A finished utterance ``{"transcript": ...}`` -> ordered TextParts."""
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
        """Voice the reply's text. The target keeps the reply on its origin."""
        text = " ".join(p.text for p in as_parts(parts) if isinstance(p, TextPart)).strip()
        if not text:
            return
        await self.engine.speak(text)

    async def send_with_id(self, target: DeliveryTarget, message: str) -> str | None:
        await self.engine.speak(message)
        return None


__all__ = ["VoiceAdapter"]
