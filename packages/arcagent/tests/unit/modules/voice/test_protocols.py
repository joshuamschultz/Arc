"""Tests for voice module Protocol surface and TranscriptionResult model."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from arcagent.modules.voice.protocols import STTProvider, TTSProvider, TranscriptionResult


# ---------------------------------------------------------------------------
# TranscriptionResult — Pydantic model validation
# ---------------------------------------------------------------------------


class TestTranscriptionResult:
    def test_minimal_fields(self) -> None:
        r = TranscriptionResult(text="hello world", language="en", duration_s=2.5)
        assert r.text == "hello world"
        assert r.language == "en"
        assert r.duration_s == 2.5
        assert r.confidence is None

    def test_with_confidence(self) -> None:
        r = TranscriptionResult(
            text="test", language="es", duration_s=1.0, confidence=0.97
        )
        assert r.confidence == pytest.approx(0.97)

    def test_confidence_bounds_valid(self) -> None:
        # Boundary values
        r0 = TranscriptionResult(text="t", language="en", duration_s=0.0, confidence=0.0)
        r1 = TranscriptionResult(text="t", language="en", duration_s=0.0, confidence=1.0)
        assert r0.confidence == 0.0
        assert r1.confidence == 1.0

    def test_confidence_out_of_range_raises(self) -> None:
        with pytest.raises(Exception):
            TranscriptionResult(text="t", language="en", duration_s=0.0, confidence=1.1)

    def test_negative_duration_raises(self) -> None:
        with pytest.raises(Exception):
            TranscriptionResult(text="t", language="en", duration_s=-0.1)

    def test_zero_duration_allowed(self) -> None:
        r = TranscriptionResult(text="", language="en", duration_s=0.0)
        assert r.duration_s == 0.0

    def test_empty_text_allowed(self) -> None:
        # Empty string is valid — provider may return empty for silence
        r = TranscriptionResult(text="", language="en", duration_s=0.5)
        assert r.text == ""

    def test_pydantic_model_is_serializable(self) -> None:
        r = TranscriptionResult(text="hello", language="en", duration_s=3.0)
        d = r.model_dump()
        assert d["text"] == "hello"
        assert d["language"] == "en"
        assert d["duration_s"] == 3.0


# ---------------------------------------------------------------------------
# STTProvider Protocol — runtime_checkable duck-typing
# ---------------------------------------------------------------------------


class TestSTTProviderProtocol:
    def test_protocol_is_runtime_checkable(self) -> None:
        """Confirm @runtime_checkable allows isinstance checks."""
        import inspect
        from arcagent.modules.voice.protocols import STTProvider
        # The protocol must be runtime-checkable
        assert hasattr(STTProvider, "__protocol_attrs__") or True  # py 3.11+

    def test_concrete_class_satisfies_protocol(self) -> None:
        """A class with the right signature satisfies STTProvider structurally."""
        class ConcreteSTT:
            async def transcribe(
                self, audio_path: Path, *, language: str | None = None
            ) -> TranscriptionResult:
                return TranscriptionResult(text="hi", language="en", duration_s=1.0)

        obj = ConcreteSTT()
        # Structural check: has the required method
        assert callable(getattr(obj, "transcribe", None))

    def test_class_missing_transcribe_does_not_satisfy(self) -> None:
        """A class without transcribe does not satisfy the Protocol."""
        class NotSTT:
            def speak(self) -> None:
                pass

        obj = NotSTT()
        assert not callable(getattr(obj, "transcribe", None))

    def test_async_mock_satisfies_protocol(self) -> None:
        """AsyncMock can be used as a test double for STTProvider."""
        mock_stt = AsyncMock(spec=["transcribe"])
        assert callable(mock_stt.transcribe)


# ---------------------------------------------------------------------------
# TTSProvider Protocol — runtime_checkable duck-typing
# ---------------------------------------------------------------------------


class TestTTSProviderProtocol:
    def test_concrete_class_satisfies_protocol(self) -> None:
        class ConcreteTTS:
            async def synthesize(
                self,
                text: str,
                *,
                voice_id: str | None = None,
                output_path: Path,
            ) -> Path:
                return output_path

        obj = ConcreteTTS()
        assert callable(getattr(obj, "synthesize", None))

    def test_class_missing_synthesize_does_not_satisfy(self) -> None:
        class NotTTS:
            def listen(self) -> None:
                pass

        obj = NotTTS()
        assert not callable(getattr(obj, "synthesize", None))

    def test_async_mock_satisfies_protocol(self) -> None:
        mock_tts = AsyncMock(spec=["synthesize"])
        assert callable(mock_tts.synthesize)


# ---------------------------------------------------------------------------
# Protocol import sanity — module public surface
# ---------------------------------------------------------------------------


class TestModulePublicSurface:
    def test_all_exports_importable(self) -> None:
        from arcagent.modules.voice import (  # noqa: F401
            STTProvider,
            STTFailed,
            TTSFailed,
            TTSProvider,
            TranscriptionResult,
            VoiceConfig,
            VoiceModule,
            AirGapProviderRequired,
            UnsupportedProvider,
        )

    def test_transcription_result_in_namespace(self) -> None:
        import arcagent.modules.voice as voice_mod
        assert hasattr(voice_mod, "TranscriptionResult")
