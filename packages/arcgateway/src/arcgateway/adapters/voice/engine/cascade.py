"""CascadeEngine — the default VoiceEngine (SPEC-077 COMP-003, D-761).

Composes an STT and a TTS behind the seam. It deliberately does NOT hold the
agent: dispatch of a transcript to the agent, and delivery of the reply, are the
adapter's and the gateway's job (SPEC-065 REQ-310). So ``speak`` voices exactly
the text it is given — thin-face by construction (REQ-005).

A sub-engine that is absent (model not installed / not configured) degrades to a
typed :class:`VoiceEngineUnavailableError` the adapter can turn into a spoken "voice
unavailable", never an import crash at startup.
"""

from __future__ import annotations

from arcgateway.adapters.voice.engine.base import STTEngine, TTSEngine


class VoiceEngineUnavailableError(RuntimeError):
    """A sub-engine needed for this operation is not configured/available."""


class CascadeEngine:
    """Default cascade: ``listen`` via STT, ``speak`` via TTS."""

    def __init__(self, *, stt: STTEngine | None, tts: TTSEngine | None) -> None:
        self._stt = stt
        self._tts = tts

    async def listen(self, audio: bytes) -> str:
        if self._stt is None:
            raise VoiceEngineUnavailableError("no speech-to-text engine configured")
        return await self._stt.listen(audio)

    async def speak(self, text: str) -> bytes:
        if self._tts is None:
            raise VoiceEngineUnavailableError("no text-to-speech engine configured")
        return await self._tts.speak(text)

    async def aclose(self) -> None:
        for sub in (self._stt, self._tts):
            close = getattr(sub, "aclose", None)
            if callable(close):
                await close()


__all__ = ["CascadeEngine", "VoiceEngineUnavailableError"]
