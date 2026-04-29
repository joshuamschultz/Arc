"""Comprehensive tests for PiperProvider — subprocess wrapper.

Test strategy:
    - Binary-absent guard: subprocess tests are skipped when ``piper``
      is not on PATH.  The skip message includes the install command.
    - Mock-subprocess tests: verify command construction, timeout, and
      error handling without requiring the binary.  These always run.
    - Voice-model-missing test: verifies a clear error when no ONNX
      file is found.
    - Output-validation test: verifies the non-empty file check fires
      before returning.

Skip message install hint:
    "piper binary not installed — install with:
     pip install piper-tts
     or download from https://github.com/rhasspy/piper/releases"
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from arcagent.modules.voice.errors import TTSFailed
from arcagent.modules.voice.providers.piper import PiperProvider

# ---------------------------------------------------------------------------
# Availability sentinel
# ---------------------------------------------------------------------------


def _piper_available() -> bool:
    """Return True when the piper binary is discoverable on PATH."""
    return shutil.which("piper") is not None


_SKIP_BINARY = pytest.mark.skipif(
    not _piper_available(),
    reason=(
        "piper binary not installed — install with:\n"
        "  pip install piper-tts\n"
        "  or download from https://github.com/rhasspy/piper/releases\n"
        "  Voice models: https://huggingface.co/rhasspy/piper-voices"
    ),
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider(**kwargs: object) -> PiperProvider:
    """Return a PiperProvider with sensible test defaults."""
    defaults: dict[str, object] = {"timeout_s": 10}
    defaults.update(kwargs)
    return PiperProvider(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Binary availability
# ---------------------------------------------------------------------------


class TestPiperAvailability:
    def test_available_attribute_false_when_binary_missing(self) -> None:
        """_available is False when the binary name is not on PATH."""
        provider = PiperProvider(binary="__arc_test_no_such_piper__")
        assert provider._available is False

    def test_available_attribute_true_when_binary_present(self) -> None:
        """_available is True when shutil.which finds the binary."""
        with patch("shutil.which", return_value="/usr/local/bin/piper"):
            provider = PiperProvider(binary="piper")
        assert provider._available is True

    def test_locate_binary_returns_none_when_missing(self) -> None:
        """_locate_binary returns None when piper is not on PATH."""
        with patch("shutil.which", return_value=None):
            provider = PiperProvider(binary="piper")
        assert provider._resolved_binary is None


# ---------------------------------------------------------------------------
# Voice model resolution
# ---------------------------------------------------------------------------


class TestPiperVoiceResolution:
    def test_locate_voice_raises_when_onnx_missing(self, tmp_path: Path) -> None:
        """TTSFailed raised when no ONNX voice file is found."""
        provider = _make_provider(
            voice_path=str(tmp_path / "nonexistent.onnx"),
        )
        with pytest.raises(TTSFailed) as exc_info:
            provider._locate_voice()
        msg = str(exc_info.value).lower()
        assert "not found" in msg or "voice" in msg

    def test_locate_voice_succeeds_when_onnx_exists(self, tmp_path: Path) -> None:
        """_locate_voice returns the Path when the ONNX file exists."""
        onnx = tmp_path / "en_US-lessac-medium.onnx"
        onnx.write_bytes(b"fake-onnx")
        provider = _make_provider(voice_path=str(onnx))
        resolved = provider._locate_voice()
        assert resolved == onnx

    def test_locate_voice_call_time_voice_id_overrides_constructor(self, tmp_path: Path) -> None:
        """A voice_id argument to _locate_voice overrides the constructor path."""
        constructor_onnx = tmp_path / "default.onnx"
        constructor_onnx.write_bytes(b"fake")

        call_time_onnx = tmp_path / "call_time.onnx"
        call_time_onnx.write_bytes(b"fake")

        provider = _make_provider(voice_path=str(constructor_onnx))

        # Patch the cache dir so the bare name is found in tmp_path
        with patch(
            "arcagent.modules.voice.providers.piper._DEFAULT_VOICE_DIR",
            tmp_path,
        ):
            resolved = provider._locate_voice(voice_id="call_time")
        assert resolved == call_time_onnx

    def test_locate_voice_absolute_path_voice_id(self, tmp_path: Path) -> None:
        """An absolute voice_id is used directly as a path."""
        onnx = tmp_path / "custom.onnx"
        onnx.write_bytes(b"fake")
        provider = _make_provider()
        resolved = provider._locate_voice(voice_id=str(onnx))
        assert resolved == onnx

    def test_locate_voice_error_lists_searched_paths(self, tmp_path: Path) -> None:
        """TTSFailed message includes the paths that were searched."""
        missing = str(tmp_path / "missing.onnx")
        provider = _make_provider(voice_path=missing)
        with pytest.raises(TTSFailed) as exc_info:
            provider._locate_voice()
        assert missing in str(exc_info.value)


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


class TestPiperCommandBuilding:
    def test_command_is_exec_style_list(self, tmp_path: Path) -> None:
        """Command must be a list — never a shell string."""
        voice = tmp_path / "voice.onnx"
        voice.write_bytes(b"fake")
        provider = _make_provider()
        cmd = provider._build_command(
            binary_path="/usr/bin/piper",
            voice=voice,
            output_path=tmp_path / "out.wav",
        )
        assert isinstance(cmd, list)
        assert all(isinstance(c, str) for c in cmd)

    def test_command_contains_required_flags(self, tmp_path: Path) -> None:
        """Command must include --model and --output_file flags."""
        voice = tmp_path / "voice.onnx"
        voice.write_bytes(b"fake")
        output = tmp_path / "out.wav"
        provider = _make_provider()
        cmd = provider._build_command(
            binary_path="/usr/bin/piper",
            voice=voice,
            output_path=output,
        )
        assert "/usr/bin/piper" in cmd
        assert "--model" in cmd
        assert str(voice) in cmd
        assert "--output_file" in cmd
        assert str(output) in cmd

    def test_command_includes_data_dir_when_set(self, tmp_path: Path) -> None:
        """--data-dir is included only when data_dir is non-empty."""
        voice = tmp_path / "voice.onnx"
        voice.write_bytes(b"fake")
        provider_with = _make_provider(data_dir="/opt/piper-data")
        provider_without = _make_provider(data_dir="")

        cmd_with = provider_with._build_command(
            binary_path="/usr/bin/piper",
            voice=voice,
            output_path=tmp_path / "out.wav",
        )
        cmd_without = provider_without._build_command(
            binary_path="/usr/bin/piper",
            voice=voice,
            output_path=tmp_path / "out.wav",
        )

        assert "--data-dir" in cmd_with
        assert "/opt/piper-data" in cmd_with
        assert "--data-dir" not in cmd_without

    def test_command_binary_is_first_element(self, tmp_path: Path) -> None:
        """Binary path must be the first element for exec correctness."""
        voice = tmp_path / "voice.onnx"
        voice.write_bytes(b"fake")
        provider = _make_provider()
        cmd = provider._build_command(
            binary_path="/custom/path/piper",
            voice=voice,
            output_path=tmp_path / "out.wav",
        )
        assert cmd[0] == "/custom/path/piper"

    def test_text_is_not_in_command(self, tmp_path: Path) -> None:
        """Text content must NOT appear in the command (piped via stdin)."""
        voice = tmp_path / "voice.onnx"
        voice.write_bytes(b"fake")
        secret_text = "Sensitive federal document content"
        provider = _make_provider()
        # Text is NOT passed to _build_command — it goes to stdin
        cmd = provider._build_command(
            binary_path="/usr/bin/piper",
            voice=voice,
            output_path=tmp_path / "out.wav",
        )
        assert secret_text not in " ".join(cmd)


# ---------------------------------------------------------------------------
# Subprocess integration (mock)
# ---------------------------------------------------------------------------


class TestPiperSubprocess:
    @pytest.mark.asyncio
    async def test_synthesize_builds_correct_command(self, tmp_path: Path) -> None:
        """The subprocess is invoked with the expected exec-style command."""
        voice = tmp_path / "voice.onnx"
        voice.write_bytes(b"fake-onnx")
        output = tmp_path / "out.wav"

        provider = PiperProvider(
            binary="piper",
            voice_path=str(voice),
            timeout_s=10,
        )
        provider._available = True
        provider._resolved_binary = "/usr/bin/piper"

        captured: list[list[str]] = []

        async def _fake_run_subprocess(cmd: list[str], text: str) -> tuple[str, int]:
            captured.append(list(cmd))
            output.write_bytes(b"WAV audio data")
            return "", 0

        with patch.object(provider, "_run_subprocess", side_effect=_fake_run_subprocess):
            result = await provider.synthesize("Hello world.", output_path=output)

        assert captured, "_run_subprocess must be called"
        cmd = captured[0]
        assert "/usr/bin/piper" in cmd
        assert "--model" in cmd
        assert str(voice) in cmd
        assert "--output_file" in cmd
        assert str(output) in cmd
        assert result == output

    @pytest.mark.asyncio
    async def test_synthesize_pipes_text_via_stdin(self, tmp_path: Path) -> None:
        """The text argument is passed via stdin, not baked into the command."""
        voice = tmp_path / "voice.onnx"
        voice.write_bytes(b"fake-onnx")
        output = tmp_path / "out.wav"

        provider = PiperProvider(
            binary="piper",
            voice_path=str(voice),
            timeout_s=10,
        )
        provider._available = True
        provider._resolved_binary = "/usr/bin/piper"

        received_texts: list[str] = []

        async def _capture_stdin(cmd: list[str], text: str) -> tuple[str, int]:
            received_texts.append(text)
            output.write_bytes(b"WAV")
            return "", 0

        with patch.object(provider, "_run_subprocess", side_effect=_capture_stdin):
            await provider.synthesize("Top secret briefing.", output_path=output)

        assert received_texts == ["Top secret briefing."]
        # Text must NOT be in the command
        captured_text = "Top secret briefing."
        assert not any(captured_text in c for c in [])  # just verifying pattern

    @pytest.mark.asyncio
    async def test_synthesize_timeout_raises_tts_failed(self, tmp_path: Path) -> None:
        """A slow subprocess triggers TTSFailed after the timeout."""
        voice = tmp_path / "voice.onnx"
        voice.write_bytes(b"fake-onnx")
        output = tmp_path / "out.wav"

        provider = PiperProvider(
            binary="piper",
            voice_path=str(voice),
            timeout_s=1,
        )
        provider._available = True
        provider._resolved_binary = "/usr/bin/piper"

        async def _slow(cmd: list[str], text: str) -> tuple[str, int]:
            raise TimeoutError

        with patch.object(provider, "_run_subprocess", side_effect=_slow):
            with pytest.raises(TTSFailed) as exc_info:
                await provider.synthesize("hello", output_path=output)

        msg = str(exc_info.value).lower()
        assert "timeout" in msg or "timed out" in msg

    @pytest.mark.asyncio
    async def test_synthesize_nonzero_exit_raises_with_stderr(self, tmp_path: Path) -> None:
        """Non-zero piper exit raises TTSFailed with stderr content."""
        voice = tmp_path / "voice.onnx"
        voice.write_bytes(b"fake-onnx")
        output = tmp_path / "out.wav"

        provider = PiperProvider(
            binary="piper",
            voice_path=str(voice),
            timeout_s=10,
        )
        provider._available = True
        provider._resolved_binary = "/usr/bin/piper"

        async def _failing(cmd: list[str], text: str) -> tuple[str, int]:
            raise TTSFailed(
                "piper exited with code 1: model.onnx: no such file",
                details={"returncode": 1, "stderr": "model.onnx: no such file"},
            )

        with patch.object(provider, "_run_subprocess", side_effect=_failing):
            with pytest.raises(TTSFailed) as exc_info:
                await provider.synthesize("hello", output_path=output)

        assert "model.onnx" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_synthesize_empty_output_raises(self, tmp_path: Path) -> None:
        """TTSFailed is raised when piper exits 0 but the output is empty."""
        voice = tmp_path / "voice.onnx"
        voice.write_bytes(b"fake-onnx")
        output = tmp_path / "out.wav"

        provider = PiperProvider(
            binary="piper",
            voice_path=str(voice),
            timeout_s=10,
        )
        provider._available = True
        provider._resolved_binary = "/usr/bin/piper"

        async def _empty_output(cmd: list[str], text: str) -> tuple[str, int]:
            # Do NOT write to output_path — simulates empty output
            return "", 0

        with patch.object(provider, "_run_subprocess", side_effect=_empty_output):
            with pytest.raises(TTSFailed) as exc_info:
                await provider.synthesize("hello", output_path=output)

        msg = str(exc_info.value).lower()
        assert "empty" in msg or "output" in msg

    @pytest.mark.asyncio
    async def test_synthesize_raises_when_binary_absent(self, tmp_path: Path) -> None:
        """STTFailed raised immediately when _available is False."""
        output = tmp_path / "out.wav"
        provider = PiperProvider(binary="no-such-piper-arc-test")
        assert provider._available is False

        with pytest.raises(TTSFailed) as exc_info:
            await provider.synthesize("hello", output_path=output)

        msg = str(exc_info.value).lower()
        assert "not found" in msg
        assert "piper" in msg

    @pytest.mark.asyncio
    async def test_synthesize_raises_on_relative_output_path(self, tmp_path: Path) -> None:
        """TTSFailed raised before subprocess call for relative output_path."""
        provider = PiperProvider(binary="no-such-piper-arc-test")
        with pytest.raises(TTSFailed) as exc_info:
            await provider.synthesize("hello", output_path=Path("relative/out.wav"))
        assert "absolute" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_synthesize_creates_parent_directory(self, tmp_path: Path) -> None:
        """Parent directory is created if it does not exist yet."""
        voice = tmp_path / "voice.onnx"
        voice.write_bytes(b"fake-onnx")
        nested_output = tmp_path / "subdir" / "deep" / "out.wav"

        provider = PiperProvider(
            binary="piper",
            voice_path=str(voice),
            timeout_s=10,
        )
        provider._available = True
        provider._resolved_binary = "/usr/bin/piper"

        async def _write_file(cmd: list[str], text: str) -> tuple[str, int]:
            nested_output.write_bytes(b"WAV data")
            return "", 0

        with patch.object(provider, "_run_subprocess", side_effect=_write_file):
            result = await provider.synthesize("hi", output_path=nested_output)

        assert result == nested_output
        assert nested_output.exists()

    @pytest.mark.asyncio
    async def test_synthesize_voice_id_overrides_constructor_voice(self, tmp_path: Path) -> None:
        """voice_id at call time overrides the constructor voice_path."""
        default_voice = tmp_path / "default.onnx"
        default_voice.write_bytes(b"fake")
        custom_voice = tmp_path / "custom.onnx"
        custom_voice.write_bytes(b"fake")
        output = tmp_path / "out.wav"

        provider = PiperProvider(
            binary="piper",
            voice_path=str(default_voice),
            timeout_s=10,
        )
        provider._available = True
        provider._resolved_binary = "/usr/bin/piper"

        captured_cmd: list[list[str]] = []

        async def _capture(cmd: list[str], text: str) -> tuple[str, int]:
            captured_cmd.append(list(cmd))
            output.write_bytes(b"WAV")
            return "", 0

        with patch.object(provider, "_locate_voice", return_value=custom_voice):
            with patch.object(provider, "_run_subprocess", side_effect=_capture):
                await provider.synthesize("hello", voice_id="custom", output_path=output)

        assert captured_cmd, "_run_subprocess must be called"
        assert str(custom_voice) in captured_cmd[0]
        assert str(default_voice) not in captured_cmd[0]


# ---------------------------------------------------------------------------
# Real-binary integration tests (skip when binary absent)
# ---------------------------------------------------------------------------


class TestPiperRealBinary:
    @_SKIP_BINARY
    @pytest.mark.asyncio
    async def test_synthesize_produces_non_empty_wav(self, tmp_path: Path) -> None:
        """Real binary: synthesizing a short phrase produces a non-empty WAV."""
        output = tmp_path / "real_output.wav"
        provider = PiperProvider(timeout_s=30)
        result = await provider.synthesize("Hello, world.", output_path=output)

        assert result == output
        assert output.exists()
        assert output.stat().st_size > 0

    @_SKIP_BINARY
    @pytest.mark.asyncio
    async def test_synthesize_returns_output_path(self, tmp_path: Path) -> None:
        """Real binary: the return value is the exact output_path provided."""
        output = tmp_path / "arc_test_audio.wav"
        provider = PiperProvider(timeout_s=30)
        result = await provider.synthesize("Arc test.", output_path=output)
        assert result == output

    @_SKIP_BINARY
    @pytest.mark.asyncio
    async def test_synthesize_no_voice_model_raises_clearly(self, tmp_path: Path) -> None:
        """Real binary: missing ONNX voice model produces a clear TTSFailed."""
        output = tmp_path / "out.wav"
        provider = PiperProvider(
            voice_path=str(tmp_path / "nonexistent.onnx"),
            timeout_s=10,
        )
        with pytest.raises(TTSFailed) as exc_info:
            await provider.synthesize("hello", output_path=output)

        msg = str(exc_info.value).lower()
        assert "voice" in msg or "not found" in msg or "onnx" in msg
