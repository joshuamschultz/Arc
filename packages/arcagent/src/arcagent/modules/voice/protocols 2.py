"""STT/TTS Provider Protocols for the voice module.

Defines the structural-subtyping contracts that every provider must satisfy.
Providers need NOT inherit from these classes — duck-typing via
``@runtime_checkable`` Protocol is sufficient.

Dataclass:
    TranscriptionResult — validated result from STT

Protocol:
    STTProvider — async transcribe(audio_path, ...) -> TranscriptionResult
    TTSProvider  — async synthesize(text, ..., output_path) -> Path
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class TranscriptionResult(BaseModel):
    """Validated result from a speech-to-text transcription.

    ``confidence`` is optional because not all providers expose a score
    (e.g. ElevenLabs TTS providers, basic Whisper invocations).
    The ``text`` field must NEVER be logged raw at federal/enterprise tiers —
    callers are responsible for hashing before audit emission.
    """

    text: str
    language: str
    duration_s: float = Field(ge=0.0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


@runtime_checkable
class STTProvider(Protocol):
    """Protocol for speech-to-text providers.

    All implementations must be safe to call from asyncio — any blocking
    I/O (subprocess, file reads) must be wrapped with
    ``asyncio.get_event_loop().run_in_executor``.
    """

    async def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
    ) -> TranscriptionResult:
        """Transcribe audio file to text.

        Args:
            audio_path: Absolute path to the audio file.
            language: BCP-47 language hint (e.g. "en", "es").
                      If None, the provider auto-detects.

        Returns:
            TranscriptionResult with text, detected language, duration,
            and optional confidence score.

        Raises:
            STTFailed: Provider call failed or returned an unusable result.
        """
        ...  # pragma: no cover


@runtime_checkable
class TTSProvider(Protocol):
    """Protocol for text-to-speech providers.

    Implementations must write audio to ``output_path`` and return the same
    path on success. This makes the tool's return value unambiguous.
    """

    async def synthesize(
        self,
        text: str,
        *,
        voice_id: str | None = None,
        output_path: Path,
    ) -> Path:
        """Synthesize text to an audio file.

        Args:
            text: Plaintext to synthesize (already PII-redacted at federal/enterprise).
            voice_id: Provider-specific voice identifier. None uses the provider's
                      default voice.
            output_path: Destination path for the generated audio file.

        Returns:
            The same ``output_path`` after the file has been written.

        Raises:
            TTSFailed: Synthesis failed or the provider returned an error.
        """
        ...  # pragma: no cover
