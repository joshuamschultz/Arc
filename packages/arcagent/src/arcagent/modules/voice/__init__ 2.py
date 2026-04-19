"""Voice module — STT/TTS provider Protocols with multiple plugin backends.

Provides:
    VoiceModule       — main facade; registers transcribe + synthesize tools
    STTProvider       — Protocol for speech-to-text providers
    TTSProvider       — Protocol for text-to-speech providers
    TranscriptionResult — Pydantic result model
    VoiceConfig       — module configuration with tier-driven defaults
    AirGapProviderRequired — raised when federal tier uses a cloud provider

Air-gap path (no network, safe for SCIFs):
    WhisperCppProvider (STT) + PiperProvider (TTS)

Cloud path (requires credentials):
    WhisperApiProvider (STT) + ElevenLabsProvider (TTS)

Federal tier enforcement:
    AirGapProviderRequired is raised at VoiceModule construction if a
    cloud provider is configured. This is a hard error — there is no
    silent fallback.

Spec: SPEC-018, Task T4.7
"""

from arcagent.modules.voice.config import VoiceConfig
from arcagent.modules.voice.errors import (
    AirGapProviderRequired,
    STTFailed,
    TTSFailed,
    UnsupportedProvider,
    VoiceError,
)
from arcagent.modules.voice.protocols import STTProvider, TranscriptionResult, TTSProvider
from arcagent.modules.voice.voice_module import VoiceModule

__all__ = [
    "AirGapProviderRequired",
    "STTFailed",
    "STTProvider",
    "TTSFailed",
    "TTSProvider",
    "TranscriptionResult",
    "UnsupportedProvider",
    "VoiceConfig",
    "VoiceError",
    "VoiceModule",
]
