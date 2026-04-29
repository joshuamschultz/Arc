"""SPEC-021 Task 3.4 — voice module decorator-form tests.

The new ``capabilities.py`` exposes:

  * Two ``@tool`` callables — ``transcribe`` and ``synthesize``.
  * Two ``@hook`` subscribers — ``voice.transcribe.request`` and
    ``voice.synthesize.request``.

This file verifies:

  1. The two tools and two hooks register via :class:`CapabilityLoader`
     against the voice module directory.
  2. Hook events match the names declared in MODULE.yaml.
  3. ``transcribe`` and ``synthesize`` route through the configured
     provider plugins and emit the expected audit events.
  4. Federal-tier cloud-provider misconfiguration is rejected at
     ``_runtime.configure()`` time.

Legacy :class:`VoiceModule` tests in ``test_voice_module.py`` continue
to verify behaviour at the wrapper level.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.core.capability_loader import CapabilityLoader
from arcagent.core.capability_registry import CapabilityRegistry
from arcagent.modules.voice import _runtime
from arcagent.modules.voice.errors import AirGapProviderRequired
from arcagent.modules.voice.protocols import TranscriptionResult


@pytest.fixture(autouse=True)
def _reset_runtime() -> Iterator[None]:
    _runtime.reset()
    yield
    _runtime.reset()


@pytest.fixture
def configured() -> MagicMock:
    """Configure the runtime with a personal-tier mock telemetry sink."""
    telemetry = MagicMock()
    _runtime.configure(
        config={"tier": "personal", "stt_provider": "whisper_cpp", "tts_provider": "piper"},
        telemetry=telemetry,
    )
    return telemetry


@pytest.fixture
def configured_with_redaction() -> MagicMock:
    """Enterprise tier so ``effective_redact_pii`` is True."""
    telemetry = MagicMock()
    _runtime.configure(
        config={"tier": "enterprise", "stt_provider": "whisper_cpp", "tts_provider": "piper"},
        telemetry=telemetry,
    )
    return telemetry


@pytest.mark.asyncio
class TestLoaderRegistration:
    async def test_two_tools_and_two_hooks_register(self) -> None:
        from arcagent.modules.voice import capabilities as voice_caps

        module_dir = Path(voice_caps.__file__).parent
        reg = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=[("voice", module_dir)], registry=reg)
        await loader.scan_and_register()

        # Tools
        assert await reg.get_tool("transcribe") is not None
        assert await reg.get_tool("synthesize") is not None

        # Hooks
        transcribe_hooks = await reg.get_hooks("voice.transcribe.request")
        synthesize_hooks = await reg.get_hooks("voice.synthesize.request")
        assert any(h.meta.name == "on_transcribe_request" for h in transcribe_hooks)
        assert any(h.meta.name == "on_synthesize_request" for h in synthesize_hooks)

    async def test_tool_classifications(self) -> None:
        from arcagent.modules.voice.capabilities import synthesize, transcribe

        # Both tools touch external state (subprocess / network / disk).
        assert transcribe._arc_capability_meta.classification == "state_modifying"  # type: ignore[attr-defined]
        assert synthesize._arc_capability_meta.classification == "state_modifying"  # type: ignore[attr-defined]

    async def test_hook_events_match_module_yaml(self) -> None:
        from arcagent.modules.voice.capabilities import (
            on_synthesize_request,
            on_transcribe_request,
        )

        assert on_transcribe_request._arc_capability_meta.event == "voice.transcribe.request"  # type: ignore[attr-defined]
        assert on_synthesize_request._arc_capability_meta.event == "voice.synthesize.request"  # type: ignore[attr-defined]


@pytest.mark.asyncio
class TestRuntimeContract:
    async def test_unconfigured_raises(self) -> None:
        from arcagent.modules.voice.capabilities import transcribe

        with pytest.raises(RuntimeError, match="before runtime is configured"):
            await transcribe(audio_path="/tmp/nope.wav")

    async def test_federal_with_cloud_stt_raises(self) -> None:
        with pytest.raises(AirGapProviderRequired):
            _runtime.configure(
                config={
                    "tier": "federal",
                    "stt_provider": "whisper_api",
                    "tts_provider": "piper",
                },
            )

    async def test_federal_with_cloud_tts_raises(self) -> None:
        with pytest.raises(AirGapProviderRequired):
            _runtime.configure(
                config={
                    "tier": "federal",
                    "stt_provider": "whisper_cpp",
                    "tts_provider": "elevenlabs",
                },
            )

    async def test_provider_selected_audit_emitted(self) -> None:
        telemetry = MagicMock()
        _runtime.configure(
            config={"tier": "personal", "stt_provider": "whisper_cpp", "tts_provider": "piper"},
            telemetry=telemetry,
        )
        # First call must be voice.provider_selected.
        first_call = telemetry.audit_event.call_args_list[0]
        assert first_call.args[0] == "voice.provider_selected"


@pytest.mark.asyncio
class TestTranscribeTool:
    async def test_returns_json_with_text_language_duration(
        self, configured: MagicMock, tmp_path: Path
    ) -> None:
        from arcagent.modules.voice.capabilities import transcribe

        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")

        mock_stt = AsyncMock()
        mock_stt.transcribe = AsyncMock(
            return_value=TranscriptionResult(text="hello world", language="en", duration_s=1.5)
        )
        _runtime.state().stt = mock_stt

        raw = await transcribe(audio_path=str(audio))
        result = json.loads(raw)
        assert result["text"] == "hello world"
        assert result["language"] == "en"
        assert result["duration_s"] == 1.5

    async def test_emits_audit_event_with_hash_not_text(
        self, configured: MagicMock, tmp_path: Path
    ) -> None:
        from arcagent.modules.voice.capabilities import transcribe

        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")

        mock_stt = AsyncMock()
        mock_stt.transcribe = AsyncMock(
            return_value=TranscriptionResult(text="secret content", language="en", duration_s=1.0)
        )
        _runtime.state().stt = mock_stt

        await transcribe(audio_path=str(audio))

        # Find the voice.transcribed audit call.
        transcribed_calls = [
            c for c in configured.audit_event.call_args_list if c.args[0] == "voice.transcribed"
        ]
        assert len(transcribed_calls) == 1
        payload = transcribed_calls[0].args[1]
        assert "transcript_hash" in payload
        assert "secret content" not in str(payload)

    async def test_redacts_pii_at_enterprise_tier(
        self, configured_with_redaction: MagicMock, tmp_path: Path
    ) -> None:
        from arcagent.modules.voice.capabilities import transcribe

        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")

        mock_stt = AsyncMock()
        mock_stt.transcribe = AsyncMock(
            return_value=TranscriptionResult(
                text="my SSN is 123-45-6789", language="en", duration_s=1.0
            )
        )
        _runtime.state().stt = mock_stt

        raw = await transcribe(audio_path=str(audio))
        result = json.loads(raw)
        assert "123-45-6789" not in result["text"]
        assert "[SSN]" in result["text"]


@pytest.mark.asyncio
class TestSynthesizeTool:
    async def test_returns_json_with_audio_path(
        self, configured: MagicMock, tmp_path: Path
    ) -> None:
        from arcagent.modules.voice.capabilities import synthesize

        async def _fake_synth(
            text: str,
            *,
            voice_id: str | None = None,
            output_path: Path,
        ) -> Path:
            output_path.write_bytes(b"audio bytes")
            return output_path

        mock_tts = AsyncMock()
        mock_tts.synthesize = _fake_synth
        _runtime.state().tts = mock_tts

        raw = await synthesize(text="hello world")
        result = json.loads(raw)
        assert "audio_path" in result
        assert result["audio_path"].endswith(".mp3")

    async def test_emits_audit_event(self, configured: MagicMock, tmp_path: Path) -> None:
        from arcagent.modules.voice.capabilities import synthesize

        async def _fake_synth(
            text: str,
            *,
            voice_id: str | None = None,
            output_path: Path,
        ) -> Path:
            output_path.write_bytes(b"audio bytes")
            return output_path

        mock_tts = AsyncMock()
        mock_tts.synthesize = _fake_synth
        _runtime.state().tts = mock_tts

        await synthesize(text="hello world")

        synthesized_calls = [
            c for c in configured.audit_event.call_args_list if c.args[0] == "voice.synthesized"
        ]
        assert len(synthesized_calls) == 1
        payload = synthesized_calls[0].args[1]
        assert "text_hash" in payload
        assert "hello world" not in str(payload)


@pytest.mark.asyncio
class TestHookDispatch:
    async def test_transcribe_request_hook_invokes_pipeline(
        self, configured: MagicMock, tmp_path: Path
    ) -> None:
        from arcagent.modules.voice.capabilities import on_transcribe_request

        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")

        mock_stt = AsyncMock()
        mock_stt.transcribe = AsyncMock(
            return_value=TranscriptionResult(text="hello", language="en", duration_s=1.0)
        )
        _runtime.state().stt = mock_stt

        ctx = SimpleNamespace(data={"audio_path": str(audio)})
        await on_transcribe_request(ctx)

        result: dict[str, Any] = ctx.data["result"]
        assert result["text"] == "hello"

    async def test_transcribe_request_hook_skips_when_no_audio_path(
        self, configured: MagicMock
    ) -> None:
        from arcagent.modules.voice.capabilities import on_transcribe_request

        ctx = SimpleNamespace(data={})
        await on_transcribe_request(ctx)
        assert "result" not in ctx.data

    async def test_synthesize_request_hook_invokes_pipeline(
        self, configured: MagicMock, tmp_path: Path
    ) -> None:
        from arcagent.modules.voice.capabilities import on_synthesize_request

        out_path = tmp_path / "out.mp3"

        async def _fake_synth(
            text: str,
            *,
            voice_id: str | None = None,
            output_path: Path,
        ) -> Path:
            output_path.write_bytes(b"audio")
            return output_path

        mock_tts = AsyncMock()
        mock_tts.synthesize = _fake_synth
        _runtime.state().tts = mock_tts

        ctx = SimpleNamespace(data={"text": "hello", "output_path": str(out_path)})
        await on_synthesize_request(ctx)

        result: dict[str, Any] = ctx.data["result"]
        assert result["audio_path"] == str(out_path)

    async def test_synthesize_request_hook_skips_when_no_text(self, configured: MagicMock) -> None:
        from arcagent.modules.voice.capabilities import on_synthesize_request

        ctx = SimpleNamespace(data={})
        await on_synthesize_request(ctx)
        assert "result" not in ctx.data
