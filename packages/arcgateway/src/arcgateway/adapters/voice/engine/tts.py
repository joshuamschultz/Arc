"""PiperTTS — the real text-to-speech (SPEC-077 COMP-005, REQ-005/022).

A Piper implementation of the ``TTSEngine`` seam. Piper is local, ONNX-based,
fast and ARM-friendly (verified on the DGX), and its voice model is the swappable
"Olivia voice" — clone/replace by pointing at a different ``.onnx`` voice.

``speak`` voices exactly the text it is given (thin-face, REQ-005) and returns
self-describing WAV bytes. The WAV header is built by hand rather than via
``wave`` in write mode, because the adapter contract-surface guard forbids the
storage-write idiom anywhere in a platform folder — and here the bytes never touch
disk anyway. The heavy import is lazy; model load runs the artifact verifier first.
"""

from __future__ import annotations

import asyncio
import struct
from pathlib import Path
from typing import Any

from arcgateway.adapters.voice.engine.verify import (
    ArtifactVerifier,
    ModelArtifact,
    digest_file,
)

# Conforms structurally to arcgateway.adapters.voice.engine.base.TTSEngine.


def wav_bytes(pcm: bytes, sample_rate: int, *, channels: int = 1, sample_width: int = 2) -> bytes:
    """Wrap raw PCM-16 in a minimal WAV container (no disk, no wave-writer)."""
    byte_rate = sample_rate * channels * sample_width
    block_align = channels * sample_width
    fmt = struct.pack(
        "<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, sample_width * 8
    )
    header = (
        b"RIFF"
        + struct.pack("<I", 36 + len(pcm))
        + b"WAVE"
        + b"fmt "
        + fmt
        + b"data"
        + struct.pack("<I", len(pcm))
    )
    return header + pcm


class PiperTTS:
    """Piper behind the TTSEngine seam. Returns WAV bytes of the given text."""

    def __init__(
        self,
        *,
        voice_path: str,
        expected_sha256: str | None = None,
        verifier: ArtifactVerifier | None = None,
    ) -> None:
        self._voice_path = voice_path
        self._expected = expected_sha256
        self._verifier = verifier or ArtifactVerifier()
        self._voice: Any | None = None

    def _load(self) -> Any:
        if self._voice is not None:
            return self._voice
        computed = digest_file(self._voice_path)
        self._verifier.verify(
            ModelArtifact(
                name="piper-voice",
                version=Path(self._voice_path).stem,
                sha256=computed,
                signature_ok=self._expected is None or computed == self._expected,
            )
        )
        from piper import PiperVoice  # lazy: optional [voice] dependency

        self._voice = PiperVoice.load(self._voice_path)
        return self._voice

    def _synthesize(self, text: str) -> bytes:
        voice = self._load()
        pcm = bytearray()
        for chunk in voice.synthesize(text):
            pcm.extend(chunk.audio_int16_bytes)
        return wav_bytes(bytes(pcm), voice.config.sample_rate)

    async def speak(self, text: str) -> bytes:
        return await asyncio.to_thread(self._synthesize, text)

    async def aclose(self) -> None:
        self._voice = None


__all__ = ["PiperTTS", "wav_bytes"]
