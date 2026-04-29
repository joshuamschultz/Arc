"""Per-agent voice module runtime context.

The voice module's tools and hooks share state — the resolved
``VoiceConfig``, lazy-loaded STT/TTS provider plugins, and the
telemetry sink for audit events. Decorator-stamped functions can't
carry that state in a closure, so it lives in a module-level
:class:`_State` instance configured by the agent at startup.

Mirrors the pattern in :mod:`arcagent.modules.policy._runtime` and
:mod:`arcagent.builtins.capabilities._runtime` (single-agent-per-process
model).

Tier enforcement runs at :func:`configure` time so misconfiguration
is caught before any audio is processed — fail fast, fail loud
(matches legacy ``VoiceModule.__init__`` behaviour).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from arcagent.modules.voice.config import VoiceConfig, is_cloud_stt, is_cloud_tts
from arcagent.modules.voice.errors import AirGapProviderRequired
from arcagent.modules.voice.protocols import STTProvider, TTSProvider

_logger = logging.getLogger("arcagent.modules.voice._runtime")


@dataclass
class _State:
    """Mutable runtime state shared across voice tools and hooks."""

    config: VoiceConfig
    telemetry: Any
    stt: STTProvider | None = None
    tts: TTSProvider | None = None


_state: _State | None = None


def configure(
    *,
    config: dict[str, Any] | None = None,
    telemetry: Any = None,
) -> None:
    """Bind module state. Called once at agent startup.

    Validates federal-tier air-gap policy and emits provider-selection
    audit events, matching legacy :class:`VoiceModule.__init__`.
    """
    global _state
    cfg = VoiceConfig(**(config or {}))
    _enforce_tier_policy(cfg)
    _state = _State(config=cfg, telemetry=telemetry)
    _emit_provider_selected_audit(cfg, telemetry)
    _logger.info(
        "voice: runtime configured tier=%s stt=%s tts=%s air_gap=%s redact_pii=%s",
        cfg.tier,
        cfg.stt_provider,
        cfg.tts_provider,
        cfg.effective_air_gap,
        cfg.effective_redact_pii,
    )


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    if _state is None:
        raise RuntimeError(
            "voice module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return _state


def reset() -> None:
    """Test-only: clear runtime state."""
    global _state
    _state = None


def get_stt_provider() -> STTProvider:
    """Return (and lazily create) the configured STT provider."""
    st = state()
    if st.stt is None:
        st.stt = _build_stt_provider(st.config)
    return st.stt


def get_tts_provider() -> TTSProvider:
    """Return (and lazily create) the configured TTS provider."""
    st = state()
    if st.tts is None:
        st.tts = _build_tts_provider(st.config)
    return st.tts


# --- Tier enforcement -----------------------------------------------------


def _enforce_tier_policy(cfg: VoiceConfig) -> None:
    """Raise AirGapProviderRequired if federal tier uses a cloud provider."""
    if cfg.tier != "federal":
        return

    stt = cfg.stt_provider.lower()
    if is_cloud_stt(stt):
        raise AirGapProviderRequired(
            configured_provider=stt,
            details={
                "tier": "federal",
                "configured_stt": stt,
                "allowed": ["whisper_cpp"],
            },
        )

    tts = cfg.tts_provider.lower()
    if is_cloud_tts(tts):
        raise AirGapProviderRequired(
            configured_provider=tts,
            details={
                "tier": "federal",
                "configured_tts": tts,
                "allowed": ["piper"],
            },
        )


def _emit_provider_selected_audit(cfg: VoiceConfig, telemetry: Any) -> None:
    """Emit voice.provider_selected (and cloud warning at non-federal)."""
    if telemetry is None:
        return

    stt = cfg.stt_provider.lower()
    tts = cfg.tts_provider.lower()
    tier = cfg.tier

    telemetry.audit_event(
        "voice.provider_selected",
        {
            "tier": tier,
            "stt_provider": stt,
            "tts_provider": tts,
            "air_gap": cfg.effective_air_gap,
        },
    )

    if tier != "federal" and (is_cloud_stt(stt) or is_cloud_tts(tts)):
        telemetry.audit_event(
            "voice.cloud_provider_warning",
            {
                "tier": tier,
                "stt_provider": stt,
                "tts_provider": tts,
                "warning": (
                    "Cloud voice provider selected — audio data may leave "
                    "the local environment. Review for privacy compliance."
                ),
            },
        )


# --- Provider factories ---------------------------------------------------
#
# Path-vs-name detection accepts os.sep (platform-native), forward slash,
# or backslash — bare names like ``base.en`` fall through to the
# provider's default cache lookup.


def _looks_like_path(s: str) -> bool:
    import os

    return os.sep in s or "/" in s or "\\" in s


def _build_stt_provider(cfg: VoiceConfig) -> STTProvider:
    """Instantiate the configured STT provider."""
    name = cfg.stt_provider.lower()

    if name in ("whisper_cpp",):
        from arcagent.modules.voice.providers.whisper_cpp import WhisperCppProvider

        model_path = cfg.whisper_cpp_model if _looks_like_path(cfg.whisper_cpp_model) else None
        return WhisperCppProvider(
            binary=cfg.whisper_cpp_binary,
            model_path=model_path,
            threads=cfg.whisper_cpp_threads,
            timeout_s=cfg.transcribe_timeout_s,
        )

    if name in ("whisper_api", "openai_whisper"):
        from arcagent.modules.voice.providers.whisper_api import WhisperApiProvider

        return WhisperApiProvider(
            api_key_env=cfg.openai_api_key_env,
            model=cfg.openai_whisper_model,
            timeout_s=cfg.transcribe_timeout_s,
        )

    from arcagent.modules.voice.errors import UnsupportedProvider

    raise UnsupportedProvider(name)


def _build_tts_provider(cfg: VoiceConfig) -> TTSProvider:
    """Instantiate the configured TTS provider."""
    name = cfg.tts_provider.lower()

    if name in ("piper",):
        from arcagent.modules.voice.providers.piper import PiperProvider

        voice_path = cfg.piper_model if _looks_like_path(cfg.piper_model) else None
        return PiperProvider(
            binary=cfg.piper_binary,
            voice_path=voice_path,
            data_dir=cfg.piper_data_dir,
            timeout_s=cfg.synthesize_timeout_s,
        )

    if name in ("elevenlabs",):
        from arcagent.modules.voice.providers.elevenlabs import ElevenLabsProvider

        return ElevenLabsProvider(
            api_key_env=cfg.elevenlabs_api_key_env,
            base_url=cfg.elevenlabs_base_url,
            default_voice_id=cfg.elevenlabs_default_voice_id,
            timeout_s=cfg.synthesize_timeout_s,
        )

    from arcagent.modules.voice.errors import UnsupportedProvider

    raise UnsupportedProvider(name)


__all__ = [
    "configure",
    "get_stt_provider",
    "get_tts_provider",
    "reset",
    "state",
]
