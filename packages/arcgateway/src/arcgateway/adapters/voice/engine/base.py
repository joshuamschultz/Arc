"""The VoiceEngine seam (SPEC-077 COMP-003, D-761).

The voice engine is the swappable boundary between audio and the agent. Its
default is a **cascade**: speech-to-text -> the Arc agent -> text-to-speech. The
agent is the only author of content; the engine's ``speak`` voices exactly the
text it is given (thin-face, REQ-005). PersonaPlex, if ever enabled, implements
the same :class:`VoiceEngine` Protocol as a single full-duplex component.

Optional model dependencies (Whisper, a TTS, PersonaPlex) live behind this
boundary only — they never leak into the adapter or any public contract type.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class STTEngine(Protocol):
    """Hears the operator: one utterance of audio -> its transcript."""

    async def listen(self, audio: bytes) -> str: ...


@runtime_checkable
class TTSEngine(Protocol):
    """Speaks for the agent: the agent's text -> audio, and nothing it authored."""

    async def speak(self, text: str) -> bytes: ...


@runtime_checkable
class VoiceEngine(STTEngine, TTSEngine, Protocol):
    """The full seam the adapter depends on: listen, speak, and orderly close."""

    async def aclose(self) -> None: ...


class FakeVoiceEngine:
    """Deterministic engine for tests — no model, no audio hardware.

    ``listen`` returns a fixed transcript; ``speak`` records the exact text it was
    asked to voice (so a test can prove thin-face) and returns placeholder bytes.
    """

    def __init__(self, transcript: str = "") -> None:
        self.transcript = transcript
        self.spoken: list[str] = []
        self.closed = False

    async def listen(self, audio: bytes) -> str:
        return self.transcript

    async def speak(self, text: str) -> bytes:
        self.spoken.append(text)
        return text.encode("utf-8") or b"\x00"

    async def aclose(self) -> None:
        self.closed = True


__all__ = ["FakeVoiceEngine", "STTEngine", "TTSEngine", "VoiceEngine"]
