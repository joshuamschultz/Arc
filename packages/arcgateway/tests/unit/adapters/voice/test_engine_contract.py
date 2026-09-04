"""T-001 (RED) — the VoiceEngine seam contract (SPEC-077 COMP-003, REQ-004/005).

The engine is the swappable boundary. Its default is a cascade (STT -> agent ->
TTS), but the seam itself must be a typed Protocol with a Fake that stands in for
tests, so the adapter never depends on a real model to be exercised.
"""

from __future__ import annotations

from arcgateway.adapters.voice.engine.base import (
    FakeVoiceEngine,
    STTEngine,
    TTSEngine,
    VoiceEngine,
)


def test_fake_engine_satisfies_every_seam_protocol() -> None:
    fake = FakeVoiceEngine()
    assert isinstance(fake, STTEngine)
    assert isinstance(fake, TTSEngine)
    assert isinstance(fake, VoiceEngine)


async def test_listen_returns_the_transcript() -> None:
    fake = FakeVoiceEngine(transcript="hey olivia what's on my calendar")
    assert await fake.listen(b"\x00\x01\x02") == "hey olivia what's on my calendar"


async def test_speak_voices_exactly_the_given_text_thin_face() -> None:
    """REQ-005: thin-face — the engine voices the agent's text, it does not author."""
    fake = FakeVoiceEngine()
    audio = await fake.speak("on it")
    assert isinstance(audio, bytes)
    assert audio, "speak must return non-empty audio"
    assert fake.spoken == ["on it"]


async def test_aclose_is_idempotent() -> None:
    fake = FakeVoiceEngine()
    await fake.aclose()
    await fake.aclose()
    assert fake.closed is True
