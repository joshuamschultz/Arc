"""Piper TTS provider — air-gap, subprocess-based.

Shells out to the ``piper`` binary. If the binary is absent, raises
``TTSFailed`` with clear installation guidance.

Binary install options:
    - ``pip install piper-tts`` — installs the ``piper`` entry-point.
    - GitHub releases: https://github.com/rhasspy/piper/releases
      Download the platform-specific archive and place the ``piper``
      binary on PATH.

Voice model resolution order:
    1. ``voice_id`` kwarg to :meth:`synthesize`.
    2. ``voice_path`` constructor argument (absolute ONNX path).
    3. ``~/.cache/piper/voices/<voice_id>.onnx`` (standard cache location).
    4. ``~/.cache/piper/voices/en_US-libritts-high.onnx`` (default).

Piper invocation::

    piper --model <voice.onnx> --output_file <output.wav>
    [reads plaintext from stdin]

Security:
    - Text is piped via stdin — no shell interpolation risk.
    - Output path validated before write (must be absolute).
    - No network calls; safe for air-gap / SCIF deployments.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from arcagent.modules.voice.errors import TTSFailed

_logger = logging.getLogger("arcagent.modules.voice.providers.piper")

_DEFAULT_TIMEOUT_S = 30

# Default ONNX voice model location
_DEFAULT_VOICE_DIR = Path.home() / ".cache" / "piper" / "voices"
_DEFAULT_VOICE_NAME = "en_US-libritts-high.onnx"
_DEFAULT_VOICE_PATH = _DEFAULT_VOICE_DIR / _DEFAULT_VOICE_NAME


class PiperProvider:
    """TTS provider using the Piper binary (air-gap, no network).

    Satisfies the TTSProvider Protocol via duck-typing.

    Attributes:
        _available: True when the piper binary is found at construction.
                    Used by skip-guards in tests and VoiceModule health
                    checks.
    """

    def __init__(
        self,
        binary: str = "piper",
        voice_path: str | None = None,
        data_dir: str = "",
        timeout_s: int = _DEFAULT_TIMEOUT_S,
    ) -> None:
        """Initialise the provider.

        Args:
            binary: Binary name or absolute path. Resolved against PATH if
                    not already absolute.
            voice_path: Absolute path to the default ONNX voice model.
                        ``None`` defers resolution to :meth:`_locate_voice`
                        at synthesis time.
            data_dir: Optional data directory passed to Piper with
                      ``--data-dir``. Useful when voice models reside in a
                      non-standard location alongside their ``.json``
                      config files.
            timeout_s: Maximum wall-clock seconds before the subprocess is
                       killed and ``TTSFailed`` is raised.
        """
        self._binary_name = binary
        self._voice_path_override = voice_path
        self._data_dir = data_dir
        self._timeout_s = timeout_s

        # Resolve binary once at construction so we can expose _available.
        self._resolved_binary: str | None = self._locate_binary()
        self._available: bool = self._resolved_binary is not None

    # ------------------------------------------------------------------
    # Binary + voice model resolution
    # ------------------------------------------------------------------

    def _locate_binary(self) -> str | None:
        """Search for the piper binary on PATH.

        Returns:
            Absolute binary path if found, else ``None``.
        """
        return shutil.which(self._binary_name)

    def _locate_voice(self, voice_id: str | None = None) -> Path:
        """Resolve and validate the ONNX voice model file path.

        Resolution order:
            1. ``voice_id`` kwarg (treated as a filename without extension
               relative to the voice cache dir, or as an absolute path if
               it starts with ``/``).
            2. Constructor ``voice_path`` override.
            3. ``~/.cache/piper/voices/<voice_id>.onnx`` when ``voice_id``
               is provided but not an absolute path.
            4. Default: ``~/.cache/piper/voices/en_US-libritts-high.onnx``.

        Args:
            voice_id: Optional voice identifier.  Interpreted as:
                - An absolute path when it starts with ``/``.
                - A bare model name (``en_US-lessac-medium``) otherwise —
                  appended with ``.onnx`` and searched under the cache dir.

        Returns:
            Resolved :class:`Path` to the ONNX file if it exists.

        Raises:
            TTSFailed: Voice model not found at any candidate location.
        """
        candidates: list[Path] = []

        if voice_id:
            vid_path = Path(voice_id)
            if vid_path.is_absolute():
                candidates.append(vid_path)
            else:
                # Bare name → look in the default cache directory
                name = voice_id if voice_id.endswith(".onnx") else f"{voice_id}.onnx"
                candidates.append(_DEFAULT_VOICE_DIR / name)

        if self._voice_path_override:
            candidates.append(Path(self._voice_path_override))

        candidates.append(_DEFAULT_VOICE_PATH)

        for candidate in candidates:
            if candidate.exists():
                return candidate

        searched = ", ".join(str(c) for c in candidates)
        raise TTSFailed(
            f"Piper voice model not found. Searched: {searched}. "
            "Download voices from https://huggingface.co/rhasspy/piper-voices  "
            "and place the .onnx + .onnx.json files in "
            f"{_DEFAULT_VOICE_DIR}/ .",
            details={"searched_paths": [str(c) for c in candidates]},
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def synthesize(
        self,
        text: str,
        *,
        voice_id: str | None = None,
        output_path: Path,
    ) -> Path:
        """Synthesize text to a WAV file via the Piper subprocess.

        Piper reads plaintext from stdin and writes a WAV file to
        ``--output_file``.  Text is passed via stdin so no shell
        interpolation risk exists.

        Args:
            text: Plaintext to synthesize.  Must be PII-redacted upstream
                  at federal/enterprise tiers.
            voice_id: Optional Piper voice identifier (model filename
                      without ``.onnx``) or absolute ONNX path. Overrides
                      the configured default.
            output_path: Destination WAV file path (must be absolute).

        Returns:
            The same ``output_path`` after the file has been written and
            verified non-empty.

        Raises:
            TTSFailed: Binary absent, voice model missing, subprocess error,
                       empty output file, or timeout exceeded.
        """
        self._validate_output_path(output_path)

        if not self._available or self._resolved_binary is None:
            raise TTSFailed(
                f"piper binary '{self._binary_name}' not found on PATH. "
                "Install Piper TTS: pip install piper-tts  "
                "or download from https://github.com/rhasspy/piper/releases",
                details={"binary": self._binary_name},
            )

        resolved_voice = self._locate_voice(voice_id)
        cmd = self._build_command(self._resolved_binary, resolved_voice, output_path)
        _logger.debug("piper: running %s", cmd)

        try:
            await self._run_subprocess(cmd, text)
        except TimeoutError as exc:
            raise TTSFailed(
                f"piper timed out after {self._timeout_s}s",
                details={"timeout_s": self._timeout_s},
            ) from exc

        # Verify the output file exists and is non-empty
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise TTSFailed(
                "piper produced no output file or an empty file",
                details={"output_path": str(output_path)},
            )

        return output_path

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_output_path(self, output_path: Path) -> None:
        """Raise TTSFailed if output_path is not usable.

        Ensures the path is absolute and creates the parent directory so
        callers don't need to mkdir first.
        """
        if not output_path.is_absolute():
            raise TTSFailed(
                "output_path must be absolute",
                details={"output_path": str(output_path)},
            )
        # Ensure parent directory exists before subprocess writes the file
        output_path.parent.mkdir(parents=True, exist_ok=True)

    def _build_command(
        self,
        binary_path: str,
        voice: Path,
        output_path: Path,
    ) -> list[str]:
        """Build Piper command as a safe exec-style argument list.

        Piper reads text from stdin and writes WAV to ``--output_file``.
        Text is never embedded in the command to eliminate injection risk.

        Args:
            binary_path: Resolved absolute path to the piper binary.
            voice: Validated path to the ONNX voice model.
            output_path: Validated absolute destination path for the WAV
                         output.

        Returns:
            Argument list safe for :func:`asyncio.create_subprocess_exec`.
        """
        cmd = [
            binary_path,
            "--model",
            str(voice),
            "--output_file",
            str(output_path),
        ]
        if self._data_dir:
            cmd += ["--data-dir", self._data_dir]
        return cmd

    async def _run_subprocess(self, cmd: list[str], text: str) -> tuple[str, int]:
        """Pipe text to piper via stdin and return (stderr, returncode).

        Args:
            cmd: Argument list for :func:`asyncio.create_subprocess_exec`.
            text: Plaintext to pipe into piper's stdin.

        Returns:
            ``(stderr, returncode)`` tuple.

        Raises:
            TimeoutError: Process exceeded ``self._timeout_s`` seconds.
            TTSFailed: Non-zero exit code from piper.
        """
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=text.encode("utf-8")),
            timeout=self._timeout_s,
        )
        stderr = (stderr_bytes or b"").decode("utf-8", errors="replace")
        returncode = proc.returncode or 0

        if returncode != 0:
            raise TTSFailed(
                f"piper exited with code {returncode}: {stderr[:500]}",
                details={"returncode": returncode, "stderr": stderr[:500]},
            )

        return stderr, returncode
