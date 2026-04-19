"""STUB tests for WhisperCppProvider.

These tests verify that the wrapper class exists, is importable, and
exposes the correct interface. They skip if the whisper-cpp binary is
absent from PATH (which is expected in CI and dev environments).

TODO (production wiring):
    When whisper.cpp is installed, remove the skip guards and run
    the full integration suite.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from arcagent.modules.voice.providers.whisper_cpp import WhisperCppProvider

WHISPER_CPP_AVAILABLE = shutil.which("whisper-cpp") is not None
SKIP_REASON = (
    "whisper-cpp binary not found on PATH; "
    "install whisper.cpp to run these tests in non-stub mode"
)


class TestWhisperCppStub:
    def test_class_is_importable(self) -> None:
        """WhisperCppProvider must be importable regardless of binary presence."""
        assert WhisperCppProvider is not None

    def test_has_transcribe_method(self) -> None:
        provider = WhisperCppProvider()
        assert callable(getattr(provider, "transcribe", None))

    def test_transcribe_is_coroutine(self) -> None:
        provider = WhisperCppProvider()
        # Should be an async method
        assert asyncio.iscoroutinefunction(provider.transcribe)

    def test_binary_not_found_raises_stt_failed(
        self, tmp_path: Path
    ) -> None:
        """When binary is absent, transcribe raises STTFailed with guidance."""
        from arcagent.modules.voice.errors import STTFailed

        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")

        provider = WhisperCppProvider(binary="this-binary-does-not-exist-arc-test")

        async def _run() -> None:
            await provider.transcribe(audio)

        with pytest.raises(STTFailed) as exc_info:
            asyncio.run(_run())

        # Error message must provide installation guidance
        assert "not found" in str(exc_info.value).lower()
        assert "whisper" in str(exc_info.value).lower()

    def test_non_absolute_path_raises(self) -> None:
        from arcagent.modules.voice.errors import STTFailed

        provider = WhisperCppProvider()

        async def _run() -> None:
            await provider.transcribe(Path("relative/audio.wav"))

        with pytest.raises(STTFailed):
            asyncio.run(_run())

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        from arcagent.modules.voice.errors import STTFailed

        provider = WhisperCppProvider()

        async def _run() -> None:
            await provider.transcribe(tmp_path / "nonexistent.wav")

        with pytest.raises(STTFailed):
            asyncio.run(_run())

    def test_parse_json_output(self) -> None:
        """Parse well-formed whisper-cpp JSON output into TranscriptionResult."""
        import json

        provider = WhisperCppProvider()

        stdout = json.dumps(
            {
                "transcription": [
                    {
                        "text": "Hello there.",
                        "offsets": {"from": 0, "to": 1500},
                    }
                ],
                "language": "en",
            }
        )
        result = provider._parse_output((stdout, "", 0), Path("/tmp/audio.wav"))
        assert result.text == "Hello there."
        assert result.language == "en"
        assert result.duration_s == pytest.approx(1.5)

    def test_parse_json_output_multiple_segments(self) -> None:
        import json

        provider = WhisperCppProvider()

        stdout = json.dumps(
            {
                "transcription": [
                    {"text": "Hello", "offsets": {"from": 0, "to": 500}},
                    {"text": "world.", "offsets": {"from": 500, "to": 2000}},
                ],
                "language": "en",
            }
        )
        result = provider._parse_output((stdout, "", 0), Path("/tmp/audio.wav"))
        assert "Hello" in result.text
        assert "world." in result.text

    def test_parse_plaintext_fallback(self) -> None:
        """Non-JSON output falls back to treating stdout as plain text."""
        provider = WhisperCppProvider()
        stdout = "   This is plain text transcription.   "
        result = provider._parse_output((stdout, "", 0), Path("/tmp/audio.wav"))
        assert result.text == "This is plain text transcription."

    def test_parse_nonzero_exit_raises(self) -> None:
        from arcagent.modules.voice.errors import STTFailed

        provider = WhisperCppProvider()
        with pytest.raises(STTFailed) as exc_info:
            provider._parse_output(("", "error output", 1), Path("/tmp/audio.wav"))
        assert "exit" in str(exc_info.value).lower() or "code" in str(exc_info.value).lower()

    @pytest.mark.skipif(not WHISPER_CPP_AVAILABLE, reason=SKIP_REASON)
    @pytest.mark.asyncio
    async def test_transcribe_with_real_binary(self, tmp_path: Path) -> None:
        """Integration test — only runs when binary is present."""
        # This test intentionally uses a minimal WAV file that may produce
        # empty transcription; we verify it completes without error.
        import struct
        import wave

        wav_path = tmp_path / "test.wav"
        with wave.open(str(wav_path), "w") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            # 1 second of silence
            frames = struct.pack("<" + "h" * 16000, *([0] * 16000))
            wf.writeframes(frames)

        provider = WhisperCppProvider()
        result = await provider.transcribe(wav_path)
        assert isinstance(result.text, str)
        assert isinstance(result.language, str)
        assert result.duration_s >= 0.0
