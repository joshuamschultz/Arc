"""Tests for the voice runtime — config loading, tier enforcement,
provider building, and shutdown pool drain.

The pipeline/tool/hook surface lives in ``capabilities.py`` and is
covered by ``test_voice_capabilities.py``. This file covers the state
container (:mod:`arcagent.modules.voice._runtime`) and the config model
that production actually drives.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from arcagent.modules.voice import _runtime
from arcagent.modules.voice.config import VoiceConfig
from arcagent.modules.voice.errors import AirGapProviderRequired, UnsupportedProvider


@pytest.fixture(autouse=True)
def _reset_runtime() -> Iterator[None]:
    _runtime.reset()
    yield
    _runtime.reset()


# ---------------------------------------------------------------------------
# Federal tier enforcement (at configure time)
# ---------------------------------------------------------------------------


class TestFederalTierEnforcement:
    def test_federal_with_cloud_stt_raises(self) -> None:
        with pytest.raises(AirGapProviderRequired) as exc_info:
            _runtime.configure(
                config={
                    "tier": "federal",
                    "stt_provider": "whisper_api",
                    "tts_provider": "piper",
                }
            )
        assert "federal" in str(exc_info.value).lower()
        assert "whisper_api" in str(exc_info.value)

    def test_federal_with_openai_whisper_alias_raises(self) -> None:
        with pytest.raises(AirGapProviderRequired):
            _runtime.configure(
                config={
                    "tier": "federal",
                    "stt_provider": "openai_whisper",
                    "tts_provider": "piper",
                }
            )

    def test_federal_with_cloud_tts_raises(self) -> None:
        with pytest.raises(AirGapProviderRequired) as exc_info:
            _runtime.configure(
                config={
                    "tier": "federal",
                    "stt_provider": "whisper_cpp",
                    "tts_provider": "elevenlabs",
                }
            )
        assert "elevenlabs" in str(exc_info.value)

    def test_federal_with_airgap_providers_ok(self) -> None:
        _runtime.configure(
            config={"tier": "federal", "stt_provider": "whisper_cpp", "tts_provider": "piper"}
        )
        assert _runtime.state().config.tier == "federal"

    def test_federal_error_details_contain_allowed_providers(self) -> None:
        with pytest.raises(AirGapProviderRequired) as exc_info:
            _runtime.configure(
                config={
                    "tier": "federal",
                    "stt_provider": "whisper_api",
                    "tts_provider": "piper",
                }
            )
        details = exc_info.value.details
        assert details is not None
        assert "allowed" in details

    def test_error_code_is_correct(self) -> None:
        with pytest.raises(AirGapProviderRequired) as exc_info:
            _runtime.configure(
                config={
                    "tier": "federal",
                    "stt_provider": "whisper_api",
                    "tts_provider": "piper",
                }
            )
        assert exc_info.value.code == "VOICE_AIRGAP_REQUIRED"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestConfigLoading:
    def test_default_config(self) -> None:
        _runtime.configure()
        cfg = _runtime.state().config
        assert cfg.tier == "personal"
        assert cfg.stt_provider == "whisper_cpp"
        assert cfg.tts_provider == "piper"

    def test_config_dict_loaded(self) -> None:
        _runtime.configure(config={"tier": "enterprise", "redact_pii": True})
        cfg = _runtime.state().config
        assert cfg.tier == "enterprise"
        assert cfg.redact_pii is True

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
        assert cfg.effective_air_gap is True

    def test_effective_redact_pii_enterprise_always_true(self) -> None:
        cfg = VoiceConfig(tier="enterprise", redact_pii=False)
        assert cfg.effective_redact_pii is True


# ---------------------------------------------------------------------------
# Provider building (lazy, via runtime)
# ---------------------------------------------------------------------------


class TestProviderBuilding:
    def test_build_whisper_cpp_stt(self) -> None:
        _runtime.configure(config={"stt_provider": "whisper_cpp"})
        provider = _runtime.get_stt_provider()
        from arcagent.modules.voice.providers.whisper_cpp import WhisperCppProvider

        assert isinstance(provider, WhisperCppProvider)

    def test_build_piper_tts(self) -> None:
        _runtime.configure(config={"tts_provider": "piper"})
        provider = _runtime.get_tts_provider()
        from arcagent.modules.voice.providers.piper import PiperProvider

        assert isinstance(provider, PiperProvider)

    def test_build_whisper_api_stt(self) -> None:
        _runtime.configure(config={"stt_provider": "whisper_api"})
        provider = _runtime.get_stt_provider()
        from arcagent.modules.voice.providers.whisper_api import WhisperApiProvider

        assert isinstance(provider, WhisperApiProvider)

    def test_build_elevenlabs_tts(self) -> None:
        _runtime.configure(config={"tts_provider": "elevenlabs"})
        provider = _runtime.get_tts_provider()
        from arcagent.modules.voice.providers.elevenlabs import ElevenLabsProvider

        assert isinstance(provider, ElevenLabsProvider)

    def test_unknown_stt_provider_raises(self) -> None:
        _runtime.configure(config={"stt_provider": "whisper_cpp"})
        _runtime.state().config.stt_provider = "nonexistent_stt"
        with pytest.raises(UnsupportedProvider):
            _runtime.get_stt_provider()

    def test_unknown_tts_provider_raises(self) -> None:
        _runtime.configure(config={"tts_provider": "piper"})
        _runtime.state().config.tts_provider = "nonexistent_tts"
        with pytest.raises(UnsupportedProvider):
            _runtime.get_tts_provider()

    def test_stt_provider_lazily_initialised(self) -> None:
        _runtime.configure()
        assert _runtime.state().stt is None
        _ = _runtime.get_stt_provider()
        assert _runtime.state().stt is not None

    def test_tts_provider_lazily_initialised(self) -> None:
        _runtime.configure()
        assert _runtime.state().tts is None
        _ = _runtime.get_tts_provider()
        assert _runtime.state().tts is not None


# ---------------------------------------------------------------------------
# Shutdown pool drain
# ---------------------------------------------------------------------------


class TestShutdownDrain:
    @pytest.mark.asyncio
    async def test_aclose_drains_provider_pools(self) -> None:
        """aclose() awaits each provider's close() and clears state."""
        from unittest.mock import AsyncMock

        _runtime.configure()
        st = _runtime.state()
        stt = AsyncMock()
        tts = AsyncMock()
        st.stt = stt
        st.tts = tts

        await _runtime.aclose()

        stt.close.assert_awaited_once()
        tts.close.assert_awaited_once()
        with pytest.raises(RuntimeError, match="before runtime is configured"):
            _runtime.state()

    @pytest.mark.asyncio
    async def test_aclose_unconfigured_is_noop(self) -> None:
        await _runtime.aclose()  # must not raise

    @pytest.mark.asyncio
    async def test_aclose_skips_providers_without_close(self) -> None:
        """Air-gap providers may not expose close(); aclose() tolerates that."""
        _runtime.configure()
        st = _runtime.state()

        class _NoCloseProvider:
            pass

        st.stt = _NoCloseProvider()  # type: ignore[assignment]
        st.tts = _NoCloseProvider()  # type: ignore[assignment]

        await _runtime.aclose()  # must not raise
