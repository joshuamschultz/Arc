"""Error hierarchy for the voice module.

All voice errors extend ArcAgentError for structured audit trails.
Federal tier raises AirGapProviderRequired when a cloud provider
is configured — never silently falls back.

N818 noqa: The names STTFailed, TTSFailed, UnsupportedProvider, and
AirGapProviderRequired are part of the public API contract defined in
SPEC-018 T4.7. Renaming them to *Error would break the spec surface.
"""

from __future__ import annotations

from typing import Any

from arcagent.core.errors import ArcAgentError


class VoiceError(ArcAgentError):
    """Base error for all voice module failures."""

    _component = "voice"


class STTFailed(VoiceError):  # noqa: N818
    """Speech-to-text transcription failed."""

    def __init__(
        self,
        message: str = "Transcription failed",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code="VOICE_STT_FAILED", message=message, details=details)


class TTSFailed(VoiceError):  # noqa: N818
    """Text-to-speech synthesis failed."""

    def __init__(
        self,
        message: str = "Synthesis failed",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code="VOICE_TTS_FAILED", message=message, details=details)


class UnsupportedProvider(VoiceError):  # noqa: N818
    """Requested STT/TTS provider is not registered."""

    def __init__(
        self,
        provider: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            code="VOICE_UNSUPPORTED_PROVIDER",
            message=f"Provider '{provider}' is not supported",
            details=details,
        )


class AirGapProviderRequired(VoiceError):  # noqa: N818
    """Federal tier requires air-gap provider; cloud provider was configured.

    Federal deployments (DOE, SCIFs) must never route voice data to
    external APIs. This error is raised at module initialisation so
    misconfiguration is caught before any audio is processed.
    """

    def __init__(
        self,
        configured_provider: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            code="VOICE_AIRGAP_REQUIRED",
            message=(
                f"Federal tier requires air-gap provider; "
                f"cloud provider '{configured_provider}' is not allowed"
            ),
            details=details,
        )
