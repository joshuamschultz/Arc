"""VoiceModule — STT/TTS facade with provider routing and PII redaction.

Registers two tools in the arcagent tool registry:
    transcribe(audio_path) -> {text, language, duration_s}
    synthesize(text, voice_id) -> {audio_path}

Tier enforcement at construction:
    Federal: AirGapProviderRequired raised if a cloud provider is configured.
    Enterprise/personal: cloud or air-gap providers both allowed.

PII redaction:
    Federal/enterprise: applied bidirectionally (IN: before agent sees transcript;
    OUT: before text reaches synthesizer).
    Personal: opt-in via VoiceConfig.redact_pii.

Audit events (NEVER log raw transcript or text):
    voice.transcribed — {provider, duration_s, language, redaction_applied, transcript_hash}
    voice.synthesized — {provider, voice_id, text_hash, output_size_bytes}
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

from arcrun.types import Tool, ToolContext

from arcagent.modules.voice.config import VoiceConfig, is_cloud_stt, is_cloud_tts
from arcagent.modules.voice.errors import AirGapProviderRequired, STTFailed, TTSFailed
from arcagent.modules.voice.protocols import STTProvider, TranscriptionResult, TTSProvider
from arcagent.modules.voice.redaction import redact_transcript
from arcagent.utils.audit import safe_audit

_logger = logging.getLogger("arcagent.modules.voice")

# Maximum bytes captured from exception messages in error detail fields.
# Keeps structured logs from ballooning on large exception messages (LLM02).
_MAX_ERROR_MSG_LEN: int = 200


def _looks_like_path(s: str) -> bool:
    """Return True if the string appears to be a filesystem path.

    Accepts any of: os.sep (platform-native), forward slash, or backslash.
    This is more robust than checking only for '/' which breaks on Windows.

    Args:
        s: String to evaluate.

    Returns:
        True if the string contains os.sep, '/', or '\\'.
    """
    return os.sep in s or "/" in s or "\\" in s


class VoiceModule:
    """Voice module — provides transcribe and synthesize tools.

    Instantiated by the agent orchestrator and wired into the tool
    registry. Validates tier constraints at construction time so
    misconfiguration is caught before any audio is processed.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        telemetry: Any = None,
    ) -> None:
        self._config = VoiceConfig(**(config or {}))
        self._telemetry = telemetry

        # Enforce federal air-gap policy at construction
        self._enforce_tier_policy()

        # Lazy-initialised provider instances
        self._stt: STTProvider | None = None
        self._tts: TTSProvider | None = None

        _logger.info(
            "voice: module initialised tier=%s stt=%s tts=%s air_gap=%s redact_pii=%s",
            self._config.tier,
            self._config.stt_provider,
            self._config.tts_provider,
            self._config.effective_air_gap,
            self._config.effective_redact_pii,
        )

    # ------------------------------------------------------------------
    # Public: tool factory
    # ------------------------------------------------------------------

    def make_transcribe_tool(self) -> Tool:
        """Create the ``transcribe`` tool for registration in the tool registry."""

        async def _execute(params: dict[str, Any], ctx: ToolContext) -> str:
            import json as _json
            audio_path = Path(params["audio_path"])
            language: str | None = params.get("language")
            return _json.dumps(await self._transcribe(audio_path, language=language))

        return Tool(
            name="transcribe",
            description=(
                "Transcribe an audio file to text using the configured STT provider. "
                "Returns the transcript text, detected language, and audio duration. "
                "PII is redacted automatically at federal/enterprise tiers."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "audio_path": {
                        "type": "string",
                        "description": "Absolute path to the audio file to transcribe",
                    },
                    "language": {
                        "type": "string",
                        "description": "Optional BCP-47 language hint (e.g. 'en', 'es')",
                    },
                },
                "required": ["audio_path"],
            },
            execute=_execute,
            timeout_seconds=self._config.transcribe_timeout_s + 10,
        )

    def make_synthesize_tool(self) -> Tool:
        """Create the ``synthesize`` tool for registration in the tool registry."""

        async def _execute(params: dict[str, Any], ctx: ToolContext) -> str:
            import json as _json
            text = params["text"]
            voice_id: str | None = params.get("voice_id")
            # Generate a unique output path per call to prevent collisions
            out_name = f"arc_tts_{uuid.uuid4().hex}.mp3"
            output_path = Path(tempfile.gettempdir()) / out_name
            result = await self._synthesize(text, voice_id=voice_id, output_path=output_path)
            return _json.dumps(result)

        return Tool(
            name="synthesize",
            description=(
                "Synthesize text to an audio file using the configured TTS provider. "
                "Returns the path to the generated audio file. "
                "PII is redacted from the input text at federal/enterprise tiers."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Text to synthesize",
                    },
                    "voice_id": {
                        "type": "string",
                        "description": "Provider-specific voice identifier (optional)",
                    },
                },
                "required": ["text"],
            },
            execute=_execute,
            timeout_seconds=self._config.synthesize_timeout_s + 10,
        )

    # ------------------------------------------------------------------
    # Internal: transcription pipeline
    # ------------------------------------------------------------------

    async def _transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
    ) -> dict[str, Any]:
        """Run the full transcription pipeline with redaction and audit."""
        stt = self._get_stt_provider()

        try:
            result: TranscriptionResult = await stt.transcribe(
                audio_path, language=language
            )
        except STTFailed:
            raise
        except Exception as exc:
            raise STTFailed(
                f"Unexpected error during transcription: {type(exc).__name__}",
                details={"error": str(exc)[:_MAX_ERROR_MSG_LEN]},
            ) from exc

        # PII redaction — applied BEFORE the text reaches the agent
        text = result.text
        redaction_applied = False
        if self._config.effective_redact_pii:
            text, redaction_applied = redact_transcript(text)

        # Audit event — NEVER include raw text
        transcript_hash = hashlib.sha256(text.encode()).hexdigest()
        await safe_audit(
            self._telemetry,
            "voice.transcribed",
            {
                "provider": self._config.stt_provider,
                "duration_s": result.duration_s,
                "language": result.language,
                "redaction_applied": redaction_applied,
                "transcript_hash": transcript_hash,
            },
            logger=_logger,
        )

        return {
            "text": text,
            "language": result.language,
            "duration_s": result.duration_s,
        }

    # ------------------------------------------------------------------
    # Internal: synthesis pipeline
    # ------------------------------------------------------------------

    async def _synthesize(
        self,
        text: str,
        *,
        voice_id: str | None = None,
        output_path: Path,
    ) -> dict[str, Any]:
        """Run the full synthesis pipeline with redaction and audit."""
        tts = self._get_tts_provider()

        # PII redaction on INPUT text — applied before passing to provider
        synthesis_text = text
        if self._config.effective_redact_pii:
            synthesis_text, _ = redact_transcript(text)

        text_hash = hashlib.sha256(synthesis_text.encode()).hexdigest()

        try:
            out_path = await tts.synthesize(
                synthesis_text, voice_id=voice_id, output_path=output_path
            )
        except TTSFailed:
            raise
        except Exception as exc:
            raise TTSFailed(
                f"Unexpected error during synthesis: {type(exc).__name__}",
                details={"error": str(exc)[:_MAX_ERROR_MSG_LEN]},
            ) from exc

        output_size_bytes = out_path.stat().st_size if out_path.exists() else 0

        # Audit event — NEVER include raw text
        await safe_audit(
            self._telemetry,
            "voice.synthesized",
            {
                "provider": self._config.tts_provider,
                "voice_id": voice_id or "default",
                "text_hash": text_hash,
                "output_size_bytes": output_size_bytes,
            },
            logger=_logger,
        )

        return {"audio_path": str(out_path)}

    # ------------------------------------------------------------------
    # Internal: provider factory (lazy init)
    # ------------------------------------------------------------------

    def _get_stt_provider(self) -> STTProvider:
        """Return (and lazily create) the configured STT provider."""
        if self._stt is None:
            self._stt = self._build_stt_provider()
        return self._stt

    def _get_tts_provider(self) -> TTSProvider:
        """Return (and lazily create) the configured TTS provider."""
        if self._tts is None:
            self._tts = self._build_tts_provider()
        return self._tts

    def _build_stt_provider(self) -> STTProvider:
        """Instantiate the configured STT provider."""
        name = self._config.stt_provider.lower()

        if name in ("whisper_cpp",):
            from arcagent.modules.voice.providers.whisper_cpp import WhisperCppProvider
            # Pass model_path only when the config value looks like a filesystem
            # path. A bare name like 'base.en' is not a path — _locate_model
            # falls back to the default cache.
            _wcpp_model_cfg = self._config.whisper_cpp_model
            _wcpp_model_path = _wcpp_model_cfg if _looks_like_path(_wcpp_model_cfg) else None
            return WhisperCppProvider(
                binary=self._config.whisper_cpp_binary,
                model_path=_wcpp_model_path,
                threads=self._config.whisper_cpp_threads,
                timeout_s=self._config.transcribe_timeout_s,
            )

        if name in ("whisper_api", "openai_whisper"):
            from arcagent.modules.voice.providers.whisper_api import WhisperApiProvider
            return WhisperApiProvider(
                api_key_env=self._config.openai_api_key_env,
                model=self._config.openai_whisper_model,
                timeout_s=self._config.transcribe_timeout_s,
            )

        from arcagent.modules.voice.errors import UnsupportedProvider
        raise UnsupportedProvider(name)

    def _build_tts_provider(self) -> TTSProvider:
        """Instantiate the configured TTS provider."""
        name = self._config.tts_provider.lower()

        if name in ("piper",):
            from arcagent.modules.voice.providers.piper import PiperProvider
            # Pass voice_path only when the config value looks like a filesystem
            # path. A bare name like 'en_US-lessac-medium' is not a path —
            # _locate_voice falls back to the default cache.
            _piper_voice_cfg = self._config.piper_model
            _piper_voice_path = _piper_voice_cfg if _looks_like_path(_piper_voice_cfg) else None
            return PiperProvider(
                binary=self._config.piper_binary,
                voice_path=_piper_voice_path,
                data_dir=self._config.piper_data_dir,
                timeout_s=self._config.synthesize_timeout_s,
            )

        if name in ("elevenlabs",):
            from arcagent.modules.voice.providers.elevenlabs import ElevenLabsProvider
            return ElevenLabsProvider(
                api_key_env=self._config.elevenlabs_api_key_env,
                base_url=self._config.elevenlabs_base_url,
                default_voice_id=self._config.elevenlabs_default_voice_id,
                timeout_s=self._config.synthesize_timeout_s,
            )

        from arcagent.modules.voice.errors import UnsupportedProvider
        raise UnsupportedProvider(name)

    # ------------------------------------------------------------------
    # Internal: tier enforcement
    # ------------------------------------------------------------------

    def _enforce_tier_policy(self) -> None:
        """Raise AirGapProviderRequired if federal tier uses a cloud provider.

        This is checked at construction time so misconfiguration is caught
        before any audio data is processed — fail fast, fail loud.
        """
        if self._config.tier != "federal":
            return

        stt = self._config.stt_provider.lower()
        if is_cloud_stt(stt):
            raise AirGapProviderRequired(
                configured_provider=stt,
                details={
                    "tier": "federal",
                    "configured_stt": stt,
                    "allowed": ["whisper_cpp"],
                },
            )

        tts = self._config.tts_provider.lower()
        if is_cloud_tts(tts):
            raise AirGapProviderRequired(
                configured_provider=tts,
                details={
                    "tier": "federal",
                    "configured_tts": tts,
                    "allowed": ["piper"],
                },
            )
