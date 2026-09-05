"""Voice engine registry — engines are config-selectable leaves (SPEC-077).

Built-ins register on import; config selects by name; unknown names fail loud; a
new engine registers without touching core. Plus Kokoro blend parsing + seam
conformance.
"""

from __future__ import annotations

import pytest

from arcgateway.adapters.voice.engine.base import TTSEngine
from arcgateway.adapters.voice.engine.kokoro import KokoroTTS, parse_blend
from arcgateway.adapters.voice.engine.registry import (
    UnknownVoiceEngineError,
    build_tts,
    register_tts,
    registered_stt,
    registered_tts,
)


def test_builtin_engines_register_on_import() -> None:
    assert "piper" in registered_tts()
    assert "kokoro" in registered_tts()
    assert "whisper" in registered_stt()


def test_parse_blend() -> None:
    assert parse_blend("af_jessica:0.6,af_nicole:0.4") == [("af_jessica", 0.6), ("af_nicole", 0.4)]
    assert parse_blend("af_heart") == [("af_heart", 1.0)]
    assert parse_blend("  ") == []


def test_kokoro_satisfies_the_tts_seam() -> None:
    assert isinstance(KokoroTTS(model_path="m.onnx", voices_path="v.bin"), TTSEngine)


def test_a_new_engine_registers_and_builds_without_touching_core() -> None:
    class _FakeTTS:
        async def speak(self, text: str) -> bytes:
            return b"audio"

        async def aclose(self) -> None:
            return None

    register_tts("_test_fake")(lambda _cfg: _FakeTTS())
    engine = build_tts("_test_fake", {})
    assert isinstance(engine, TTSEngine)


def test_unknown_engine_name_fails_loudly() -> None:
    with pytest.raises(UnknownVoiceEngineError):
        build_tts("does-not-exist", {})
