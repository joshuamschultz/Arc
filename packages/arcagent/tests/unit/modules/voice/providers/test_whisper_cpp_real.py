"""Comprehensive tests for WhisperCppProvider — subprocess wrapper.

Test strategy:
    - Binary-absent guard: all tests that exercise the real subprocess are
      skipped when ``whisper-cpp`` is not on PATH.  The skip message includes
      the install command so operators know exactly what to run.
    - Mock-subprocess tests: verify command construction, timeout handling,
      and stderr capture without requiring the binary.  These always run.
    - Model-missing test: verifies a clear error when no GGML model is found.
    - Fixture WAV: ``tests/fixtures/audio/hello.wav`` — 0.5s of silence at
      16kHz mono.  Produces an empty or minimal transcript; we verify the
      call completes without error and returns a well-typed result.

Skip message install hint:
    "whisper-cpp binary not installed — install with:
     macOS: brew install whisper-cpp
     Linux: build from source at https://github.com/ggerganov/whisper.cpp"
"""

from __future__ import annotations

import asyncio
import json
import shutil
import struct
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.modules.voice.errors import STTFailed
from arcagent.modules.voice.protocols import TranscriptionResult
from arcagent.modules.voice.providers.whisper_cpp import WhisperCppProvider

# ---------------------------------------------------------------------------
# Availability sentinel
# ---------------------------------------------------------------------------

_FIXTURE_WAV = (
    Path(__file__).parent.parent.parent.parent
    / "fixtures"
    / "audio"
    / "hello.wav"
)


def _whisper_available() -> bool:
    """Return True when the whisper-cpp binary is discoverable on PATH."""
    return (
        shutil.which("whisper-cpp") is not None
        or shutil.which("whisper") is not None
    )


_SKIP_BINARY = pytest.mark.skipif(
    not _whisper_available(),
    reason=(
        "whisper-cpp binary not installed — install with:\n"
        "  macOS: brew install whisper-cpp\n"
        "  Linux: build from source at https://github.com/ggerganov/whisper.cpp\n"
        "         then run: models/download-ggml-model.sh base.en"
    ),
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider(**kwargs: object) -> WhisperCppProvider:
    """Return a WhisperCppProvider with sensible test defaults."""
    defaults: dict[str, object] = {"timeout_s": 10}
    defaults.update(kwargs)
    return WhisperCppProvider(**defaults)  # type: ignore[arg-type]


def _json_stdout(
    text: str = "Hello.",
    language: str = "en",
    to_ms: int = 1000,
    p: float | None = 0.95,
) -> str:
    """Build a minimal whisper-cpp -ojf JSON string."""
    token: dict[str, object] = {"text": text}
    if p is not None:
        token["p"] = p
    data = {
        "result": {"language": language},
        "transcription": [
            {
                "text": text,
                "offsets": {"from": 0, "to": to_ms},
                "tokens": [token],
            }
        ],
    }
    return json.dumps(data)


# ---------------------------------------------------------------------------
# Binary availability
# ---------------------------------------------------------------------------


class TestWhisperCppAvailability:
    def test_available_attribute_false_when_binary_missing(self) -> None:
        """_available must be False when the binary name does not exist."""
        provider = WhisperCppProvider(binary="__arc_test_no_such_binary__")
        assert provider._available is False

    def test_available_attribute_true_when_binary_present(self) -> None:
        """_available must be True when shutil.which finds the binary."""
        with patch("shutil.which", return_value="/usr/bin/whisper-cpp"):
            provider = WhisperCppProvider(binary="whisper-cpp")
        assert provider._available is True

    def test_locate_binary_returns_none_when_all_candidates_missing(
        self,
    ) -> None:
        """_locate_binary returns None when no candidate is on PATH."""
        with patch("shutil.which", return_value=None):
            provider = WhisperCppProvider(binary="whisper-cpp")
        assert provider._resolved_binary is None


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------


class TestWhisperCppModelResolution:
    def test_locate_model_raises_when_model_missing(self, tmp_path: Path) -> None:
        """STTFailed raised when no GGML model file is found at any location."""
        provider = _make_provider(
            model_path=str(tmp_path / "nonexistent.bin"),
        )
        with pytest.raises(STTFailed) as exc_info:
            provider._locate_model()
        msg = str(exc_info.value).lower()
        assert "not found" in msg or "model" in msg

    def test_locate_model_succeeds_when_file_exists(self, tmp_path: Path) -> None:
        """_locate_model returns the Path when the model file exists."""
        model_file = tmp_path / "ggml-base.en.bin"
        model_file.write_bytes(b"fake-ggml")
        provider = _make_provider(model_path=str(model_file))
        resolved = provider._locate_model()
        assert resolved == model_file

    def test_locate_model_call_time_arg_overrides_constructor(
        self, tmp_path: Path
    ) -> None:
        """Call-time model_path takes precedence over constructor override."""
        constructor_model = tmp_path / "constructor.bin"
        constructor_model.write_bytes(b"fake")
        call_time_model = tmp_path / "call_time.bin"
        call_time_model.write_bytes(b"fake")

        provider = _make_provider(model_path=str(constructor_model))
        resolved = provider._locate_model(model_path=str(call_time_model))
        assert resolved == call_time_model

    def test_locate_model_error_message_includes_searched_paths(
        self, tmp_path: Path
    ) -> None:
        """The STTFailed message lists every path that was searched."""
        missing = str(tmp_path / "missing.bin")
        provider = _make_provider(model_path=missing)
        with pytest.raises(STTFailed) as exc_info:
            provider._locate_model()
        assert missing in str(exc_info.value)


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


class TestWhisperCppCommandBuilding:
    def test_command_is_exec_style_list(self, tmp_path: Path) -> None:
        """Command must be a list — never a shell string."""
        model = tmp_path / "model.bin"
        model.write_bytes(b"fake")
        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"fake")

        provider = _make_provider()
        cmd = provider._build_command(
            binary_path="/usr/bin/whisper-cpp",
            audio_path=audio,
            model=model,
            language=None,
        )
        assert isinstance(cmd, list)
        assert all(isinstance(c, str) for c in cmd)

    def test_command_contains_required_flags(self, tmp_path: Path) -> None:
        """Command must include -m, -f, -ojf, and -of - flags."""
        model = tmp_path / "model.bin"
        model.write_bytes(b"fake")
        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"fake")

        provider = _make_provider()
        cmd = provider._build_command(
            binary_path="/usr/bin/whisper-cpp",
            audio_path=audio,
            model=model,
            language=None,
        )
        assert "/usr/bin/whisper-cpp" in cmd
        assert "-m" in cmd
        assert str(model) in cmd
        assert "-f" in cmd
        assert str(audio) in cmd
        # JSON-to-stdout flags
        assert "-ojf" in cmd
        assert "-of" in cmd
        assert "-" in cmd  # stdout sentinel

    def test_command_includes_language_flag_when_provided(
        self, tmp_path: Path
    ) -> None:
        """The -l flag is added only when language is specified."""
        model = tmp_path / "model.bin"
        model.write_bytes(b"fake")
        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"fake")

        provider = _make_provider()
        cmd_with_lang = provider._build_command(
            binary_path="/usr/bin/whisper-cpp",
            audio_path=audio,
            model=model,
            language="de",
        )
        cmd_no_lang = provider._build_command(
            binary_path="/usr/bin/whisper-cpp",
            audio_path=audio,
            model=model,
            language=None,
        )
        assert "-l" in cmd_with_lang
        assert "de" in cmd_with_lang
        assert "-l" not in cmd_no_lang

    def test_command_binary_is_first_element(self, tmp_path: Path) -> None:
        """Binary path must be the first element for exec correctness."""
        model = tmp_path / "model.bin"
        model.write_bytes(b"fake")
        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"fake")

        provider = _make_provider()
        cmd = provider._build_command(
            binary_path="/custom/path/whisper-cpp",
            audio_path=audio,
            model=model,
            language=None,
        )
        assert cmd[0] == "/custom/path/whisper-cpp"


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------


class TestWhisperCppOutputParsing:
    def test_parse_ojf_json_output(self) -> None:
        """Parse the -ojf JSON format into a TranscriptionResult."""
        provider = _make_provider()
        stdout = _json_stdout("Hello there.", language="en", to_ms=1500)
        result = provider._parse_output((stdout, "", 0), Path("/tmp/audio.wav"))

        assert isinstance(result, TranscriptionResult)
        assert result.text == "Hello there."
        assert result.language == "en"
        assert result.duration_s == pytest.approx(1.5)

    def test_parse_top_level_language_fallback(self) -> None:
        """Top-level 'language' key used when 'result.language' absent."""
        provider = _make_provider()
        data = {
            "transcription": [
                {"text": "Bonjour.", "offsets": {"from": 0, "to": 800}}
            ],
            "language": "fr",  # old format: language at top level
        }
        result = provider._parse_output(
            (json.dumps(data), "", 0), Path("/tmp/audio.wav")
        )
        assert result.language == "fr"

    def test_parse_multiple_segments_joined(self) -> None:
        """Multiple segment texts are joined with a space."""
        provider = _make_provider()
        data = {
            "result": {"language": "en"},
            "transcription": [
                {"text": " Hello", "offsets": {"from": 0, "to": 500}},
                {"text": " world.", "offsets": {"from": 500, "to": 1200}},
            ],
        }
        result = provider._parse_output(
            (json.dumps(data), "", 0), Path("/tmp/audio.wav")
        )
        assert "Hello" in result.text
        assert "world." in result.text

    def test_parse_duration_from_last_segment(self) -> None:
        """Duration is derived from the last segment's 'to' offset."""
        provider = _make_provider()
        data = {
            "result": {"language": "en"},
            "transcription": [
                {"text": "First.", "offsets": {"from": 0, "to": 1000}},
                {"text": "Second.", "offsets": {"from": 1000, "to": 3500}},
            ],
        }
        result = provider._parse_output(
            (json.dumps(data), "", 0), Path("/tmp/audio.wav")
        )
        assert result.duration_s == pytest.approx(3.5)

    def test_parse_confidence_averaged_from_tokens(self) -> None:
        """Confidence is the average of token 'p' fields."""
        provider = _make_provider()
        data = {
            "result": {"language": "en"},
            "transcription": [
                {
                    "text": " Test.",
                    "offsets": {"from": 0, "to": 1000},
                    "tokens": [
                        {"text": " Test", "p": 0.8},
                        {"text": ".", "p": 0.6},
                    ],
                }
            ],
        }
        result = provider._parse_output(
            (json.dumps(data), "", 0), Path("/tmp/audio.wav")
        )
        assert result.confidence is not None
        assert result.confidence == pytest.approx(0.7)

    def test_parse_confidence_none_when_no_token_probs(self) -> None:
        """Confidence is None when no tokens carry a 'p' field."""
        provider = _make_provider()
        data = {
            "result": {"language": "en"},
            "transcription": [
                {"text": " Test.", "offsets": {"from": 0, "to": 1000}, "tokens": []}
            ],
        }
        result = provider._parse_output(
            (json.dumps(data), "", 0), Path("/tmp/audio.wav")
        )
        assert result.confidence is None

    def test_parse_plaintext_fallback_on_bad_json(self) -> None:
        """Non-JSON stdout falls back to treating the output as plain text."""
        provider = _make_provider()
        result = provider._parse_output(
            ("  Plain text transcript.  ", "", 0), Path("/tmp/audio.wav")
        )
        assert result.text == "Plain text transcript."
        assert result.language == "unknown"
        assert result.duration_s == 0.0

    def test_parse_empty_plaintext_fallback_raises(self) -> None:
        """Empty output after JSON failure raises STTFailed."""
        provider = _make_provider()
        with pytest.raises(STTFailed):
            provider._parse_output(("   ", "", 0), Path("/tmp/audio.wav"))

    def test_parse_nonzero_exit_raises_with_stderr(self) -> None:
        """Non-zero returncode raises STTFailed and includes stderr."""
        provider = _make_provider()
        with pytest.raises(STTFailed) as exc_info:
            provider._parse_output(
                ("", "model file not found", 1), Path("/tmp/audio.wav")
            )
        msg = str(exc_info.value)
        assert "1" in msg  # exit code
        assert "model file not found" in msg  # stderr content


# ---------------------------------------------------------------------------
# Subprocess integration (mock)
# ---------------------------------------------------------------------------


class TestWhisperCppSubprocess:
    @pytest.mark.asyncio
    async def test_transcribe_builds_correct_command(self, tmp_path: Path) -> None:
        """The subprocess is invoked with the expected exec-style command."""
        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
        model_file = tmp_path / "model.bin"
        model_file.write_bytes(b"fake-ggml")

        provider = WhisperCppProvider(
            binary="whisper-cpp",
            model_path=str(model_file),
            threads=2,
            timeout_s=10,
        )
        provider._available = True
        provider._resolved_binary = "/usr/bin/whisper-cpp"

        captured_cmd: list[list[str]] = []

        async def _fake_run_subprocess(
            cmd: list[str],
        ) -> tuple[str, str, int]:
            captured_cmd.append(list(cmd))
            return _json_stdout("hi"), "", 0

        with patch.object(
            provider, "_run_subprocess", side_effect=_fake_run_subprocess
        ):
            await provider.transcribe(audio)

        assert captured_cmd, "subprocess must be called"
        cmd = captured_cmd[0]
        assert "/usr/bin/whisper-cpp" in cmd
        assert "-m" in cmd
        assert str(model_file) in cmd
        assert "-f" in cmd
        assert str(audio) in cmd
        assert "-ojf" in cmd
        assert "-of" in cmd
        assert "-" in cmd

    @pytest.mark.asyncio
    async def test_transcribe_passes_language_flag(self, tmp_path: Path) -> None:
        """The -l flag appears in the command when language is specified."""
        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"fake")
        model_file = tmp_path / "model.bin"
        model_file.write_bytes(b"fake")

        provider = WhisperCppProvider(
            binary="whisper-cpp",
            model_path=str(model_file),
            timeout_s=10,
        )
        provider._available = True
        provider._resolved_binary = "/usr/bin/whisper-cpp"

        captured_cmd: list[list[str]] = []

        async def _fake_run_subprocess(cmd: list[str]) -> tuple[str, str, int]:
            captured_cmd.append(list(cmd))
            return _json_stdout("Bonjour"), "", 0

        with patch.object(
            provider, "_run_subprocess", side_effect=_fake_run_subprocess
        ):
            await provider.transcribe(audio, language="fr")

        assert "-l" in captured_cmd[0]
        assert "fr" in captured_cmd[0]

    @pytest.mark.asyncio
    async def test_transcribe_timeout_raises_stt_failed(
        self, tmp_path: Path
    ) -> None:
        """A slow subprocess triggers STTFailed after the timeout."""
        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"fake")
        model_file = tmp_path / "model.bin"
        model_file.write_bytes(b"fake")

        provider = WhisperCppProvider(
            binary="whisper-cpp",
            model_path=str(model_file),
            timeout_s=1,
        )
        provider._available = True
        provider._resolved_binary = "/usr/bin/whisper-cpp"

        async def _slow_run_subprocess(cmd: list[str]) -> tuple[str, str, int]:
            raise asyncio.TimeoutError

        with patch.object(
            provider, "_run_subprocess", side_effect=_slow_run_subprocess
        ):
            with pytest.raises(STTFailed) as exc_info:
                await provider.transcribe(audio)

        msg = str(exc_info.value).lower()
        assert "timeout" in msg or "timed out" in msg

    @pytest.mark.asyncio
    async def test_transcribe_nonzero_exit_captures_stderr(
        self, tmp_path: Path
    ) -> None:
        """Non-zero subprocess exit raises STTFailed with stderr content."""
        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"fake")
        model_file = tmp_path / "model.bin"
        model_file.write_bytes(b"fake")

        provider = WhisperCppProvider(
            binary="whisper-cpp",
            model_path=str(model_file),
            timeout_s=10,
        )
        provider._available = True
        provider._resolved_binary = "/usr/bin/whisper-cpp"

        async def _failing_run_subprocess(
            cmd: list[str],
        ) -> tuple[str, str, int]:
            return "", "ggml_init_cublas: no CUDA device found", 1

        with patch.object(
            provider, "_run_subprocess", side_effect=_failing_run_subprocess
        ):
            with pytest.raises(STTFailed) as exc_info:
                await provider.transcribe(audio)

        assert "ggml_init_cublas" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_transcribe_raises_when_binary_absent(
        self, tmp_path: Path
    ) -> None:
        """STTFailed is raised immediately when _available is False."""
        audio = tmp_path / "audio.wav"
        audio.write_bytes(b"fake")

        provider = WhisperCppProvider(binary="no-such-binary-arc-test")
        assert provider._available is False

        with pytest.raises(STTFailed) as exc_info:
            await provider.transcribe(audio)

        msg = str(exc_info.value).lower()
        assert "not found" in msg
        # Install hint must be present
        assert "brew" in msg or "whisper" in msg

    @pytest.mark.asyncio
    async def test_transcribe_raises_on_relative_path(self, tmp_path: Path) -> None:
        """STTFailed raised before subprocess call for relative audio_path."""
        provider = WhisperCppProvider(binary="no-such-binary-arc-test")
        with pytest.raises(STTFailed):
            await provider.transcribe(Path("relative/path.wav"))

    @pytest.mark.asyncio
    async def test_transcribe_raises_on_missing_file(self, tmp_path: Path) -> None:
        """STTFailed raised when the audio file does not exist."""
        provider = WhisperCppProvider(binary="no-such-binary-arc-test")
        with pytest.raises(STTFailed):
            await provider.transcribe(tmp_path / "nonexistent.wav")


# ---------------------------------------------------------------------------
# Real-binary integration tests (skip when binary absent)
# ---------------------------------------------------------------------------


class TestWhisperCppRealBinary:
    @_SKIP_BINARY
    @pytest.mark.asyncio
    async def test_transcribe_silence_completes(self) -> None:
        """Real binary: transcribing a silent WAV completes without error."""
        assert _FIXTURE_WAV.exists(), f"Fixture WAV not found: {_FIXTURE_WAV}"

        provider = WhisperCppProvider(timeout_s=120)
        result = await provider.transcribe(_FIXTURE_WAV)

        assert isinstance(result, TranscriptionResult)
        assert isinstance(result.text, str)
        assert isinstance(result.language, str)
        assert result.duration_s >= 0.0

    @_SKIP_BINARY
    @pytest.mark.asyncio
    async def test_transcribe_result_protocol_compliant(self) -> None:
        """Real binary: TranscriptionResult satisfies field constraints."""
        assert _FIXTURE_WAV.exists(), f"Fixture WAV not found: {_FIXTURE_WAV}"

        provider = WhisperCppProvider(timeout_s=120)
        result = await provider.transcribe(_FIXTURE_WAV)

        # Pydantic validation ensures these invariants; explicit check here
        # so test failure message is clear
        assert result.duration_s >= 0.0
        if result.confidence is not None:
            assert 0.0 <= result.confidence <= 1.0

    @_SKIP_BINARY
    @pytest.mark.asyncio
    async def test_transcribe_no_model_raises_clearly(self, tmp_path: Path) -> None:
        """Real binary: missing GGML model produces a clear STTFailed."""
        audio = _FIXTURE_WAV
        assert audio.exists(), f"Fixture WAV not found: {audio}"

        provider = WhisperCppProvider(
            model_path=str(tmp_path / "nonexistent-model.bin"),
            timeout_s=10,
        )
        with pytest.raises(STTFailed) as exc_info:
            await provider.transcribe(audio)

        msg = str(exc_info.value).lower()
        assert "model" in msg or "not found" in msg
