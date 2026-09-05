"""T-007 (RED) — the CascadeEngine (SPEC-077 COMP-003, REQ-004/005, D-761).

The default VoiceEngine composes an STT and a TTS behind the seam. It does NOT
call the agent — dispatch is the adapter/gateway's job — so ``speak`` only voices
text it is handed (thin-face). A missing sub-engine degrades to a typed error the
adapter can handle, never an import crash.
"""

from __future__ import annotations

import pytest

from arcgateway.adapters.voice.engine.base import VoiceEngine
from arcgateway.adapters.voice.engine.cascade import CascadeEngine, VoiceEngineUnavailableError


class _FakeSTT:
    def __init__(self, transcript: str) -> None:
        self.transcript = transcript
        self.closed = False

    async def listen(self, audio: bytes) -> str:
        return self.transcript

    async def aclose(self) -> None:
        self.closed = True


class _FakeTTS:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.closed = False

    async def speak(self, text: str) -> bytes:
        self.spoken.append(text)
        return text.encode("utf-8")

    async def aclose(self) -> None:
        self.closed = True


def test_cascade_satisfies_the_voice_engine_seam() -> None:
    assert isinstance(CascadeEngine(stt=_FakeSTT(""), tts=_FakeTTS()), VoiceEngine)


async def test_listen_delegates_to_stt() -> None:
    engine = CascadeEngine(stt=_FakeSTT("what's on my calendar"), tts=_FakeTTS())
    assert await engine.listen(b"\x00") == "what's on my calendar"


async def test_speak_voices_exactly_the_given_text() -> None:
    tts = _FakeTTS()
    engine = CascadeEngine(stt=_FakeSTT(""), tts=tts)
    await engine.speak("You have two meetings.")
    assert tts.spoken == ["You have two meetings."]


async def test_missing_stt_degrades_typed_not_import_error() -> None:
    engine = CascadeEngine(stt=None, tts=_FakeTTS())
    with pytest.raises(VoiceEngineUnavailableError):
        await engine.listen(b"\x00")


async def test_missing_tts_degrades_typed() -> None:
    engine = CascadeEngine(stt=_FakeSTT(""), tts=None)
    with pytest.raises(VoiceEngineUnavailableError):
        await engine.speak("hi")


async def test_aclose_closes_both_sub_engines() -> None:
    stt, tts = _FakeSTT(""), _FakeTTS()
    await CascadeEngine(stt=stt, tts=tts).aclose()
    assert stt.closed and tts.closed
