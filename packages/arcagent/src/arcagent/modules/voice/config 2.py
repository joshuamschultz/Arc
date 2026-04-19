"""Configuration for the voice module.

Loaded from ``[modules.voice]`` in arcagent.toml.

Tier-driven defaults (per AUTO-8 + SDD §2):
  - federal:    air_gap=True,  redact_pii=True   (enforced; cannot override)
  - enterprise: air_gap=False, redact_pii=True   (configurable)
  - personal:   air_gap=False, redact_pii=False  (opt-in)

Cloud providers on a federal deployment raise AirGapProviderRequired
at module construction — this is validated in VoiceModule.__init__.
"""

from __future__ import annotations

from typing import Any

from arcagent.modules.base_config import ModuleConfig

# Providers that require network access (not air-gap safe).
# Validated against tier in VoiceModule.
_CLOUD_STT_PROVIDERS: frozenset[str] = frozenset({"whisper_api", "openai_whisper"})
_CLOUD_TTS_PROVIDERS: frozenset[str] = frozenset({"elevenlabs"})

# Providers that are safe for air-gap deployment (local-only).
_AIRGAP_STT_PROVIDERS: frozenset[str] = frozenset({"whisper_cpp"})
_AIRGAP_TTS_PROVIDERS: frozenset[str] = frozenset({"piper"})


def is_cloud_stt(provider: str) -> bool:
    """Return True if the STT provider requires network access."""
    return provider.lower() in _CLOUD_STT_PROVIDERS


def is_cloud_tts(provider: str) -> bool:
    """Return True if the TTS provider requires network access."""
    return provider.lower() in _CLOUD_TTS_PROVIDERS


class VoiceConfig(ModuleConfig):
    """Voice module configuration.

    All fields have safe defaults so the module works out-of-the-box.
    Federal tier ignores ``air_gap`` and ``redact_pii`` from config —
    they are always True regardless of what TOML contains.
    """

    enabled: bool = True

    # Deployment tier — drives default for air_gap and redact_pii.
    # One of: "federal", "enterprise", "personal"
    tier: str = "personal"

    # STT provider name: "whisper_cpp" | "whisper_api"
    stt_provider: str = "whisper_cpp"

    # TTS provider name: "piper" | "elevenlabs"
    tts_provider: str = "piper"

    # True = only local/subprocess providers allowed (no network calls).
    # Federal tier: always True. Enterprise/personal: configurable.
    air_gap: bool = False

    # True = PII redacted from transcripts before passing to agent.
    # Federal/enterprise: always True. Personal: opt-in.
    redact_pii: bool = False

    # ElevenLabs configuration (cloud TTS)
    elevenlabs_api_key_env: str = "ELEVENLABS_API_KEY"
    elevenlabs_base_url: str = "https://api.elevenlabs.io/v1"
    elevenlabs_default_voice_id: str = "21m00Tcm4TlvDq8ikWAM"  # Rachel

    # OpenAI Whisper API configuration (cloud STT)
    openai_api_key_env: str = "OPENAI_API_KEY"
    openai_whisper_model: str = "whisper-1"

    # Whisper.cpp configuration (air-gap STT)
    whisper_cpp_binary: str = "whisper-cpp"  # PATH lookup
    whisper_cpp_model: str = "base.en"
    whisper_cpp_threads: int = 4

    # Piper TTS configuration (air-gap TTS)
    piper_binary: str = "piper"  # PATH lookup
    piper_model: str = "en_US-lessac-medium"
    piper_data_dir: str = ""  # empty = auto-detect from binary location

    # Transcription timeout in seconds
    transcribe_timeout_s: int = 60

    # Synthesis timeout in seconds
    synthesize_timeout_s: int = 30

    @classmethod
    def for_tier(cls, tier: str, **kwargs: Any) -> VoiceConfig:
        """Build a VoiceConfig with tier-appropriate secure defaults.

        Federal overrides: air_gap=True, redact_pii=True, stt=whisper_cpp, tts=piper.
        Enterprise overrides: redact_pii=True.
        Personal: defaults apply.
        """
        tier = tier.lower()
        base: dict[str, Any] = {"tier": tier}

        if tier == "federal":
            base.update(
                {
                    "air_gap": True,
                    "redact_pii": True,
                    "stt_provider": "whisper_cpp",
                    "tts_provider": "piper",
                }
            )
        elif tier == "enterprise":
            base.update({"redact_pii": True})

        base.update(kwargs)
        return cls(**base)

    @property
    def effective_air_gap(self) -> bool:
        """Return True if air-gap is in effect (federal always True)."""
        return self.air_gap or self.tier == "federal"

    @property
    def effective_redact_pii(self) -> bool:
        """Return True if PII redaction is in effect.

        Federal and enterprise always redact; personal uses explicit setting.
        """
        if self.tier in ("federal", "enterprise"):
            return True
        return self.redact_pii
