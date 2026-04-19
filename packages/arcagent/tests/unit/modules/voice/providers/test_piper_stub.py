"""STUB tests for PiperProvider.

These tests verify that the wrapper class exists, is importable, and
exposes the correct interface. They skip if the piper binary is absent
from PATH (which is expected in CI and dev environments).

TODO (production wiring):
    When Piper TTS is installed, remove the skip guards and run
    the full integration suite.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from arcagent.modules.voice.providers.piper import PiperProvider

PIPER_AVAILABLE = shutil.which("piper") is not None
SKIP_REASON = (
    "piper binary not found on PATH; "
    "install Piper TTS (https://github.com/rhasspy/piper) to run these tests"
)


class TestPiperStub:
    def test_class_is_importable(self) -> None:
        """PiperProvider must be importable regardless of binary presence."""
        assert PiperProvider is not None

    def test_has_synthesize_method(self) -> None:
        provider = PiperProvider()
        assert callable(getattr(provider, "synthesize", None))

    def test_synthesize_is_coroutine(self) -> None:
        provider = PiperProvider()
        assert asyncio.iscoroutinefunction(provider.synthesize)

    def test_binary_not_found_raises_tts_failed(
        self, tmp_path: Path
    ) -> None:
        """When binary is absent, synthesize raises TTSFailed with guidance."""
        from arcagent.modules.voice.errors import TTSFailed

        provider = PiperProvider(binary="this-binary-does-not-exist-arc-piper-test")
        output = tmp_path / "out.wav"

        async def _run() -> None:
            await provider.synthesize("test text", output_path=output)

        with pytest.raises(TTSFailed) as exc_info:
            asyncio.run(_run())

        assert "not found" in str(exc_info.value).lower()
        assert "piper" in str(exc_info.value).lower()

    def test_non_absolute_path_raises(self, tmp_path: Path) -> None:
        from arcagent.modules.voice.errors import TTSFailed

        provider = PiperProvider()

        async def _run() -> None:
            await provider.synthesize("test", output_path=Path("relative/out.wav"))

        with pytest.raises(TTSFailed) as exc_info:
            asyncio.run(_run())
        assert "absolute" in str(exc_info.value).lower()

    def test_build_command_structure(self, tmp_path: Path) -> None:
        """Command list must never interpolate text into shell string."""
        voice_file = tmp_path / "en_US-lessac-medium.onnx"
        voice_file.write_bytes(b"fake-onnx")
        provider = PiperProvider()
        cmd = provider._build_command(
            binary_path="/usr/bin/piper",
            voice=voice_file,
            output_path=Path("/tmp/out.wav"),
        )
        # Must be a list (exec-style), not a string with shell interpolation
        assert isinstance(cmd, list)
        assert all(isinstance(c, str) for c in cmd)
        assert "/usr/bin/piper" in cmd
        assert "--model" in cmd
        assert str(voice_file) in cmd
        assert "--output_file" in cmd
        assert "/tmp/out.wav" in cmd

    def test_build_command_with_data_dir(self, tmp_path: Path) -> None:
        voice_file = tmp_path / "en_US-lessac-medium.onnx"
        voice_file.write_bytes(b"fake-onnx")
        provider = PiperProvider(data_dir="/data/piper")
        cmd = provider._build_command(
            binary_path="/usr/bin/piper",
            voice=voice_file,
            output_path=Path("/tmp/out.wav"),
        )
        assert "--data-dir" in cmd
        assert "/data/piper" in cmd

    def test_build_command_no_data_dir_when_empty(self, tmp_path: Path) -> None:
        voice_file = tmp_path / "en_US-lessac-medium.onnx"
        voice_file.write_bytes(b"fake-onnx")
        provider = PiperProvider(data_dir="")
        cmd = provider._build_command(
            binary_path="/usr/bin/piper",
            voice=voice_file,
            output_path=Path("/tmp/out.wav"),
        )
        assert "--data-dir" not in cmd

    def test_voice_id_overrides_model(self, tmp_path: Path) -> None:
        """voice_id passed to synthesize overrides the configured model."""
        from unittest.mock import patch

        # Create a fake voice file so _locate_voice can find it
        voice_file = tmp_path / "custom_voice.onnx"
        voice_file.write_bytes(b"fake-onnx")

        provider = PiperProvider()
        captured_cmd: list[list[str]] = []
        output = tmp_path / "voice_override.wav"

        async def _fake_run_subprocess(
            cmd: list[str], text: str
        ) -> tuple[str, int]:
            captured_cmd.append(list(cmd))
            # Write a non-empty file so the existence check passes
            output.write_bytes(b"audio")
            return "", 0

        async def _run() -> None:
            # Patch both _available and _resolved_binary as instance attrs
            provider._available = True
            provider._resolved_binary = "/usr/bin/piper"
            with patch.object(
                provider, "_locate_voice", return_value=voice_file
            ):
                with patch.object(
                    provider, "_run_subprocess", side_effect=_fake_run_subprocess
                ):
                    await provider.synthesize(
                        "test", voice_id="custom_voice", output_path=output
                    )

        asyncio.run(_run())

        assert captured_cmd, "_run_subprocess should have been called"
        assert str(voice_file) in captured_cmd[0]

    @pytest.mark.skipif(not PIPER_AVAILABLE, reason=SKIP_REASON)
    @pytest.mark.asyncio
    async def test_synthesize_with_real_binary(self, tmp_path: Path) -> None:
        """Integration test — only runs when binary is present."""
        provider = PiperProvider()
        output = tmp_path / "test.wav"
        result = await provider.synthesize("Hello.", output_path=output)
        assert result == output
        assert output.exists()
        assert output.stat().st_size > 0
