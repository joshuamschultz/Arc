"""KokoroTTS — a natural, human voice (SPEC-077 COMP-005, D-761).

A Kokoro (kokoro-onnx) implementation of the ``TTSEngine`` seam: far more natural
than Piper, still fully local (onnxruntime, ARM-ok). Supports voice BLENDING — a
weighted mix of Kokoro voices — so Olivia's voice is a specific timbre (e.g.
``af_jessica:0.6,af_nicole:0.4``) rather than a stock preset.

Voices ("speaker style" vectors) and the model are per-box artifacts, verified at
load. The heavy import is lazy; ``speak`` runs off the event loop and returns WAV.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from arcgateway.adapters.voice.engine.registry import register_tts
from arcgateway.adapters.voice.engine.tts import wav_bytes
from arcgateway.adapters.voice.engine.verify import (
    ArtifactVerifier,
    ModelArtifact,
    digest_file,
)


def parse_blend(spec: str) -> list[tuple[str, float]]:
    """Parse "af_jessica:0.6,af_nicole:0.4" into [(name, weight), …]."""
    pairs: list[tuple[str, float]] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        name, _, weight = part.partition(":")
        pairs.append((name.strip(), float(weight) if weight.strip() else 1.0))
    return pairs


class KokoroTTS:
    """Kokoro behind the TTSEngine seam, with optional voice blending."""

    def __init__(
        self,
        *,
        model_path: str,
        voices_path: str,
        voice: str = "af_heart",
        blend: str = "",
        speed: float = 1.0,
        lang: str = "en-us",
        expected_sha256: str | None = None,
        verifier: ArtifactVerifier | None = None,
    ) -> None:
        self._model_path = model_path
        self._voices_path = voices_path
        self._voice = voice
        self._blend = blend
        self._speed = speed
        self._lang = lang
        self._expected = expected_sha256
        self._verifier = verifier or ArtifactVerifier()
        self._kokoro: Any | None = None
        self._style: Any = None

    def _voice_style(self, name: str) -> Any:
        import numpy as np  # lazy

        getter = getattr(self._kokoro, "get_voice_style", None) or getattr(
            self._kokoro, "get_voice", None
        )
        if getter is not None:
            return np.asarray(getter(name), dtype=np.float32)
        voices = getattr(self._kokoro, "voices", None)
        if isinstance(voices, dict) and name in voices:
            return np.asarray(voices[name], dtype=np.float32)
        raise RuntimeError(f"cannot resolve kokoro voice style {name!r}")

    def _load(self) -> Any:
        if self._kokoro is not None:
            return self._kokoro
        digest = digest_file(self._model_path)
        self._verifier.verify(
            ModelArtifact(
                name="kokoro",
                version=Path(self._model_path).stem,
                sha256=digest,
                signature_ok=self._expected is None or digest == self._expected,
            )
        )
        from kokoro_onnx import Kokoro  # lazy: optional [voice] dependency

        self._kokoro = Kokoro(self._model_path, self._voices_path)
        if self._blend:
            pairs = parse_blend(self._blend)
            total = sum(weight for _, weight in pairs) or 1.0
            mixed: Any = None
            for name, weight in pairs:
                contribution = self._voice_style(name) * (weight / total)
                mixed = contribution if mixed is None else mixed + contribution
            self._style = mixed
        else:
            self._style = self._voice
        return self._kokoro

    def _synthesize(self, text: str) -> bytes:
        import numpy as np  # lazy

        kokoro = self._load()
        samples, sample_rate = kokoro.create(
            text, voice=self._style, speed=self._speed, lang=self._lang
        )
        clipped = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
        pcm = (clipped * 32767.0).astype(np.int16).tobytes()
        return wav_bytes(pcm, int(sample_rate))

    async def speak(self, text: str) -> bytes:
        return await asyncio.to_thread(self._synthesize, text)

    async def aclose(self) -> None:
        self._kokoro = None


@register_tts("kokoro")
def _build_kokoro(config: Mapping[str, Any]) -> KokoroTTS:
    return KokoroTTS(
        model_path=str(config["model_path"]),
        voices_path=str(config["voices_path"]),
        voice=str(config.get("voice", "af_heart")),
        blend=str(config.get("blend", "")),
        speed=float(config.get("speed", 1.0)),
        lang=str(config.get("lang", "en-us")),
        expected_sha256=config.get("sha256"),
    )


__all__ = ["KokoroTTS", "parse_blend"]
