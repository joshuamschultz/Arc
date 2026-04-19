"""Tests for VoiceModule — tool registration, config loading, tier enforcement."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.modules.voice.config import VoiceConfig
from arcagent.modules.voice.errors import AirGapProviderRequired, STTFailed, TTSFailed
from arcagent.modules.voice.protocols import TranscriptionResult
from arcagent.modules.voice.voice_module import VoiceModule


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_module(
    tier: str = "personal",
    stt_provider: str = "whisper_cpp",
    tts_provider: str = "piper",
    **kwargs: Any,
) -> VoiceModule:
    cfg = {
        "tier": tier,
        "stt_provider": stt_provider,
        "tts_provider": tts_provider,
        **kwargs,
    }
    return VoiceModule(config=cfg)


# ---------------------------------------------------------------------------
# Federal tier enforcement
# ---------------------------------------------------------------------------


class TestFederalTierEnforcement:
    def test_federal_with_cloud_stt_raises(self) -> None:
        """Federal tier + cloud STT provider must raise at construction."""
        with pytest.raises(AirGapProviderRequired) as exc_info:
            make_module(tier="federal", stt_provider="whisper_api", tts_provider="piper")
        assert "federal" in str(exc_info.value).lower()
        assert "whisper_api" in str(exc_info.value)

    def test_federal_with_openai_whisper_raises(self) -> None:
        """openai_whisper alias should also be rejected at federal tier."""
        with pytest.raises(AirGapProviderRequired):
            make_module(
                tier="federal", stt_provider="openai_whisper", tts_provider="piper"
            )

    def test_federal_with_cloud_tts_raises(self) -> None:
        """Federal tier + cloud TTS provider must raise at construction."""
        with pytest.raises(AirGapProviderRequired) as exc_info:
            make_module(
                tier="federal", stt_provider="whisper_cpp", tts_provider="elevenlabs"
            )
        assert "elevenlabs" in str(exc_info.value)

    def test_federal_with_airgap_providers_ok(self) -> None:
        """Federal tier + air-gap providers should initialise without error."""
        module = make_module(
            tier="federal", stt_provider="whisper_cpp", tts_provider="piper"
        )
        assert module is not None

    def test_federal_error_details_contain_allowed_providers(self) -> None:
        """AirGapProviderRequired.details must list allowed providers."""
        with pytest.raises(AirGapProviderRequired) as exc_info:
            make_module(tier="federal", stt_provider="whisper_api", tts_provider="piper")
        details = exc_info.value.details
        assert details is not None
        assert "allowed" in details

    def test_error_code_is_correct(self) -> None:
        with pytest.raises(AirGapProviderRequired) as exc_info:
            make_module(tier="federal", stt_provider="whisper_api", tts_provider="piper")
        assert exc_info.value.code == "VOICE_AIRGAP_REQUIRED"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestConfigLoading:
    def test_default_config(self) -> None:
        module = VoiceModule()
        assert module._config.tier == "personal"
        assert module._config.stt_provider == "whisper_cpp"
        assert module._config.tts_provider == "piper"

    def test_config_dict_loaded(self) -> None:
        module = VoiceModule(config={"tier": "enterprise", "redact_pii": True})
        assert module._config.tier == "enterprise"
        assert module._config.redact_pii is True

    def test_for_tier_federal(self) -> None:
        cfg = VoiceConfig.for_tier("federal")
        assert cfg.effective_air_gap is True
        assert cfg.effective_redact_pii is True
        assert cfg.stt_provider == "whisper_cpp"
        assert cfg.tts_provider == "piper"

    def test_for_tier_enterprise(self) -> None:
        cfg = VoiceConfig.for_tier("enterprise")
        assert cfg.effective_redact_pii is True
        assert cfg.tier == "enterprise"

    def test_for_tier_personal(self) -> None:
        cfg = VoiceConfig.for_tier("personal")
        assert cfg.tier == "personal"
        assert cfg.effective_redact_pii is False  # opt-in

    def test_effective_air_gap_federal_always_true(self) -> None:
        cfg = VoiceConfig(tier="federal", air_gap=False)
        # Even if air_gap=False in config, federal tier always enforces it
        assert cfg.effective_air_gap is True

    def test_effective_redact_pii_enterprise_always_true(self) -> None:
        cfg = VoiceConfig(tier="enterprise", redact_pii=False)
        assert cfg.effective_redact_pii is True


# ---------------------------------------------------------------------------
# Tool registration surface
# ---------------------------------------------------------------------------


class TestToolRegistration:
    def test_make_transcribe_tool_returns_tool(self) -> None:
        module = make_module()
        tool = module.make_transcribe_tool()
        assert tool.name == "transcribe"
        assert tool.description
        assert "audio_path" in tool.input_schema["properties"]
        assert "audio_path" in tool.input_schema.get("required", [])

    def test_make_synthesize_tool_returns_tool(self) -> None:
        module = make_module()
        tool = module.make_synthesize_tool()
        assert tool.name == "synthesize"
        assert tool.description
        assert "text" in tool.input_schema["properties"]
        assert "text" in tool.input_schema.get("required", [])

    def test_transcribe_tool_has_timeout(self) -> None:
        module = make_module()
        tool = module.make_transcribe_tool()
        assert tool.timeout_seconds > 0

    def test_synthesize_tool_has_timeout(self) -> None:
        module = make_module()
        tool = module.make_synthesize_tool()
        assert tool.timeout_seconds > 0

    def test_transcribe_tool_language_is_optional(self) -> None:
        module = make_module()
        tool = module.make_transcribe_tool()
        required = tool.input_schema.get("required", [])
        assert "language" not in required

    def test_synthesize_tool_voice_id_is_optional(self) -> None:
        module = make_module()
        tool = module.make_synthesize_tool()
        required = tool.input_schema.get("required", [])
        assert "voice_id" not in required


# ---------------------------------------------------------------------------
# Transcription pipeline
# ---------------------------------------------------------------------------


class TestTranscribePipeline:
    @pytest.mark.asyncio
    async def test_transcribe_calls_provider(self, tmp_path: Path) -> None:
        """Transcription pipeline calls the STT provider."""
        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")

        mock_result = TranscriptionResult(
            text="hello world", language="en", duration_s=2.0
        )
        mock_stt = AsyncMock()
        mock_stt.transcribe = AsyncMock(return_value=mock_result)

        module = make_module()
        module._stt = mock_stt  # inject mock provider

        result = await module._transcribe(audio)
        assert result["text"] == "hello world"
        assert result["language"] == "en"
        assert result["duration_s"] == pytest.approx(2.0)

    @pytest.mark.asyncio
    async def test_transcribe_applies_redaction_enterprise(
        self, tmp_path: Path
    ) -> None:
        """Enterprise tier applies PII redaction to transcript."""
        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")

        # SSN in transcript should be redacted
        mock_result = TranscriptionResult(
            text="my SSN is 123-45-6789", language="en", duration_s=1.0
        )
        mock_stt = AsyncMock()
        mock_stt.transcribe = AsyncMock(return_value=mock_result)

        module = make_module(tier="enterprise", stt_provider="whisper_cpp")
        module._stt = mock_stt

        result = await module._transcribe(audio)
        assert "123-45-6789" not in result["text"]
        assert "[SSN]" in result["text"]

    @pytest.mark.asyncio
    async def test_transcribe_no_redaction_personal(self, tmp_path: Path) -> None:
        """Personal tier does NOT redact by default."""
        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")

        mock_result = TranscriptionResult(
            text="my SSN is 123-45-6789", language="en", duration_s=1.0
        )
        mock_stt = AsyncMock()
        mock_stt.transcribe = AsyncMock(return_value=mock_result)

        module = make_module(tier="personal", stt_provider="whisper_cpp")
        module._stt = mock_stt

        result = await module._transcribe(audio)
        # No redaction on personal tier by default
        assert "123-45-6789" in result["text"]

    @pytest.mark.asyncio
    async def test_transcribe_emits_audit_event(self, tmp_path: Path) -> None:
        """Transcription emits audit event with hash, not raw text."""
        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")

        mock_result = TranscriptionResult(
            text="secret content", language="en", duration_s=1.0
        )
        mock_stt = AsyncMock()
        mock_stt.transcribe = AsyncMock(return_value=mock_result)

        mock_telemetry = MagicMock()
        module = make_module()
        module._telemetry = mock_telemetry
        module._stt = mock_stt

        await module._transcribe(audio)

        mock_telemetry.audit_event.assert_called_once()
        event_name, payload = mock_telemetry.audit_event.call_args[0]
        assert event_name == "voice.transcribed"
        assert "transcript_hash" in payload
        # Raw text must NOT be in audit payload
        assert "secret content" not in str(payload)

    @pytest.mark.asyncio
    async def test_transcribe_propagates_stt_failed(self, tmp_path: Path) -> None:
        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")

        mock_stt = AsyncMock()
        mock_stt.transcribe = AsyncMock(side_effect=STTFailed("provider broke"))

        module = make_module()
        module._stt = mock_stt

        with pytest.raises(STTFailed):
            await module._transcribe(audio)


# ---------------------------------------------------------------------------
# Synthesis pipeline
# ---------------------------------------------------------------------------


class TestSynthesisPipeline:
    @pytest.mark.asyncio
    async def test_synthesize_calls_provider(self, tmp_path: Path) -> None:
        output = tmp_path / "out.mp3"

        async def _fake_synth(text: str, **kw: Any) -> Path:
            output.write_bytes(b"audio")
            return output

        mock_tts = AsyncMock()
        mock_tts.synthesize = _fake_synth

        module = make_module()
        module._tts = mock_tts

        result = await module._synthesize("hello", output_path=output)
        assert "audio_path" in result

    @pytest.mark.asyncio
    async def test_synthesize_redacts_pii_enterprise(self, tmp_path: Path) -> None:
        output = tmp_path / "out.mp3"
        captured: list[str] = []

        async def _fake_synth(text: str, **kw: Any) -> Path:
            captured.append(text)
            output.write_bytes(b"audio")
            return output

        mock_tts = AsyncMock()
        mock_tts.synthesize = _fake_synth

        module = make_module(tier="enterprise", stt_provider="whisper_cpp")
        module._tts = mock_tts

        await module._synthesize(
            "call me at 555-867-5309", output_path=output
        )
        assert captured
        # Phone number should be redacted before reaching the provider
        assert "555-867-5309" not in captured[0]

    @pytest.mark.asyncio
    async def test_synthesize_emits_audit_event(self, tmp_path: Path) -> None:
        output = tmp_path / "out.mp3"

        async def _fake_synth(text: str, **kw: Any) -> Path:
            output.write_bytes(b"audio bytes")
            return output

        mock_tts = AsyncMock()
        mock_tts.synthesize = _fake_synth

        mock_telemetry = MagicMock()
        module = make_module()
        module._telemetry = mock_telemetry
        module._tts = mock_tts

        await module._synthesize("hello world", output_path=output)

        mock_telemetry.audit_event.assert_called_once()
        event_name, payload = mock_telemetry.audit_event.call_args[0]
        assert event_name == "voice.synthesized"
        assert "text_hash" in payload
        assert "output_size_bytes" in payload
        # Raw text must NOT be in audit payload
        assert "hello world" not in str(payload)

    @pytest.mark.asyncio
    async def test_synthesize_propagates_tts_failed(self, tmp_path: Path) -> None:
        output = tmp_path / "out.mp3"

        mock_tts = AsyncMock()
        mock_tts.synthesize = AsyncMock(side_effect=TTSFailed("provider broke"))

        module = make_module()
        module._tts = mock_tts

        with pytest.raises(TTSFailed):
            await module._synthesize("text", output_path=output)


# ---------------------------------------------------------------------------
# Provider building
# ---------------------------------------------------------------------------


class TestProviderBuilding:
    def test_build_whisper_cpp_stt(self) -> None:
        module = make_module(stt_provider="whisper_cpp")
        provider = module._build_stt_provider()
        from arcagent.modules.voice.providers.whisper_cpp import WhisperCppProvider
        assert isinstance(provider, WhisperCppProvider)

    def test_build_piper_tts(self) -> None:
        module = make_module(tts_provider="piper")
        provider = module._build_tts_provider()
        from arcagent.modules.voice.providers.piper import PiperProvider
        assert isinstance(provider, PiperProvider)

    def test_build_whisper_api_stt(self) -> None:
        module = make_module(stt_provider="whisper_api")
        provider = module._build_stt_provider()
        from arcagent.modules.voice.providers.whisper_api import WhisperApiProvider
        assert isinstance(provider, WhisperApiProvider)

    def test_build_elevenlabs_tts(self) -> None:
        module = make_module(tts_provider="elevenlabs")
        provider = module._build_tts_provider()
        from arcagent.modules.voice.providers.elevenlabs import ElevenLabsProvider
        assert isinstance(provider, ElevenLabsProvider)

    def test_unknown_stt_provider_raises(self) -> None:
        module = make_module(stt_provider="whisper_cpp")
        module._config.stt_provider = "nonexistent_stt"
        from arcagent.modules.voice.errors import UnsupportedProvider
        with pytest.raises(UnsupportedProvider):
            module._build_stt_provider()

    def test_unknown_tts_provider_raises(self) -> None:
        module = make_module(tts_provider="piper")
        module._config.tts_provider = "nonexistent_tts"
        from arcagent.modules.voice.errors import UnsupportedProvider
        with pytest.raises(UnsupportedProvider):
            module._build_tts_provider()

    def test_stt_provider_lazily_initialised(self) -> None:
        module = make_module()
        assert module._stt is None
        _ = module._get_stt_provider()
        assert module._stt is not None

    def test_tts_provider_lazily_initialised(self) -> None:
        module = make_module()
        assert module._tts is None
        _ = module._get_tts_provider()
        assert module._tts is not None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _write_and_return(path: Path) -> Path:
    path.write_bytes(b"audio")
    return path
