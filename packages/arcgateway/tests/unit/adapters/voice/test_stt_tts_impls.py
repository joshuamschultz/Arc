"""T-009/010 — real STT/TTS seam impls, logic testable without models.

The end-to-end model round-trip (Piper→WAV→Whisper) is verified on the DGX
(see docs/runbooks/voice-channel-deploy.md). Here we test what runs anywhere:
structural conformance to the seam, the hand-built WAV framing, and that the
artifact verifier refuses a tampered model before any heavy import happens.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from arcgateway.adapters.voice.engine.base import STTEngine, TTSEngine
from arcgateway.adapters.voice.engine.stt import WhisperSTT
from arcgateway.adapters.voice.engine.tts import PiperTTS, wav_bytes
from arcgateway.adapters.voice.engine.verify import UnverifiedArtifactError


def test_impls_satisfy_the_seam_protocols() -> None:
    assert isinstance(WhisperSTT(), STTEngine)
    assert isinstance(PiperTTS(voice_path="unused.onnx"), TTSEngine)


def test_wav_bytes_frames_pcm_correctly() -> None:
    pcm = b"\x01\x02\x03\x04\x05\x06\x07\x08"
    out = wav_bytes(pcm, 24000)
    assert out[:4] == b"RIFF"
    assert out[8:12] == b"WAVE"
    assert b"fmt " in out and b"data" in out
    assert len(out) == 44 + len(pcm)  # standard header + payload
    # sample rate lands in the fmt chunk
    assert struct.unpack_from("<I", out, 24)[0] == 24000


def test_tts_refuses_a_tampered_voice_before_loading_piper(tmp_path: Path) -> None:
    voice = tmp_path / "voice.onnx"
    voice.write_bytes(b"pretend model weights")
    tts = PiperTTS(voice_path=str(voice), expected_sha256="0" * 64)  # wrong pin
    with pytest.raises(UnverifiedArtifactError):
        tts._load()  # runs the verifier before importing piper


def test_stt_refuses_a_tampered_model_before_loading_whisper(tmp_path: Path) -> None:
    model = tmp_path / "model.bin"
    model.write_bytes(b"pretend ctranslate2 model")
    stt = WhisperSTT(model="tiny", model_path=str(model), expected_sha256="0" * 64)
    with pytest.raises(UnverifiedArtifactError):
        stt._load()
