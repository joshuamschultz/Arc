"""WhisperSTT — the real speech-to-text (SPEC-077 COMP-004, REQ-004/022).

A faster-whisper implementation of the ``STTEngine`` seam. The heavy dependency
(``faster-whisper``) is imported lazily inside the load path, so importing this
module — and the whole voice folder — stays cheap and works without the models
installed. Verified end-to-end against a real model on the DGX (Piper→WAV→Whisper
round-trip).

Audio contract: ``listen`` takes 16 kHz mono PCM-16 bytes (the endpointer/transport
resamples the 24 kHz capture down for STT). Model load runs the artifact verifier
first (REQ-022): content digest + version are always derived; cryptographic
signing against arctrust is the deferred integration (personal/enterprise accept a
self-signed local model, matching the tier stringency model).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from arcgateway.adapters.voice.engine.registry import register_stt
from arcgateway.adapters.voice.engine.verify import (
    ArtifactVerifier,
    ModelArtifact,
    digest_file,
)

# Conforms structurally to arcgateway.adapters.voice.engine.base.STTEngine.
_SAMPLE_RATE = 16000


class WhisperSTT:
    """faster-whisper behind the STTEngine seam (16 kHz mono PCM-16 in)."""

    def __init__(
        self,
        *,
        model: str = "tiny",
        model_path: str | None = None,
        device: str = "cpu",
        compute_type: str = "int8",
        expected_sha256: str | None = None,
        verifier: ArtifactVerifier | None = None,
    ) -> None:
        self._model_name = model
        self._model_path = model_path
        self._device = device
        self._compute_type = compute_type
        self._expected = expected_sha256
        self._verifier = verifier or ArtifactVerifier()
        self._model: Any | None = None

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        if self._model_path:
            computed = digest_file(self._model_path)
            self._verifier.verify(
                ModelArtifact(
                    name=f"whisper:{self._model_name}",
                    version=self._model_name,
                    sha256=computed,
                    signature_ok=self._expected is None or computed == self._expected,
                )
            )
        from faster_whisper import WhisperModel  # lazy: optional [voice] dependency

        self._model = WhisperModel(
            self._model_path or self._model_name,
            device=self._device,
            compute_type=self._compute_type,
        )
        return self._model

    def _transcribe(self, audio: bytes) -> str:
        import numpy as np  # lazy: optional [voice] dependency

        samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _info = self._load().transcribe(samples, language="en")
        return " ".join(segment.text.strip() for segment in segments).strip()

    async def listen(self, audio: bytes) -> str:
        # Model load + inference block; keep them off the event loop.
        return await asyncio.to_thread(self._transcribe, audio)

    async def aclose(self) -> None:
        self._model = None


@register_stt("whisper")
def _build_whisper(config: Mapping[str, Any]) -> WhisperSTT:
    return WhisperSTT(
        model=str(config.get("model", "tiny")),
        model_path=config.get("model_path"),
        device=str(config.get("device", "cpu")),
        compute_type=str(config.get("compute_type", "int8")),
        expected_sha256=config.get("sha256"),
    )


__all__ = ["WhisperSTT"]
