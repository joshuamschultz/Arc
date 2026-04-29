"""Whisper.cpp STT provider — air-gap, subprocess-based.

Shells out to the ``whisper-cpp`` (or ``main`` from a whisper.cpp source
build) binary. If the binary is absent, ``_available`` is False and
``transcribe()`` raises ``STTFailed`` with clear installation guidance.

Binary resolution order:
    1. Explicit path in ``binary`` constructor argument.
    2. ``whisper-cpp`` on PATH  (homebrew: ``brew install whisper-cpp``).
    3. ``whisper.cpp/main`` on PATH  (source build).

Model resolution order:
    1. Explicit ``model_path`` kwarg to ``transcribe()``.
    2. ``~/.cache/whisper-cpp/models/ggml-base.en.bin`` (default cache dir).
    3. ``model`` string treated as a literal path if it looks like one.

JSON output mode:
    Uses ``-ojf`` (output JSON format) and ``-of -`` to write JSON to
    stdout rather than a file.  The output format is::

        {
          "systeminfo": "...",
          "model": {...},
          "params": {...},
          "result": {"language": "en"},
          "transcription": [
            {
              "timestamps": {"from": "00:00:00,000", "to": "00:00:01,500"},
              "offsets": {"from": 0, "to": 1500},
              "text": " Hello there.",
              "tokens": [{"t_dtw": -1, "text": " Hello", "p": 0.93}]
            }
          ]
        }

Security:
    - Audio path is validated (must be absolute, must exist).
    - Binary path never interpolated into shell — uses exec-style subprocess.
    - Timeout enforced to prevent runaway transcription.
    - No network calls; safe for air-gap / SCIF deployments.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path

from arcagent.modules.voice.errors import STTFailed
from arcagent.modules.voice.protocols import TranscriptionResult

_logger = logging.getLogger("arcagent.modules.voice.providers.whisper_cpp")

# Default timeout for whisper.cpp subprocess (seconds)
_DEFAULT_TIMEOUT_S = 120

# Maximum bytes captured from stderr for error messages and structured details.
# Matches _DEFAULT_TIMEOUT_S naming convention.  Kept short to avoid flooding
# structured logs with binary/ANSI noise from the whisper.cpp binary (LLM02).
_MAX_STDERR_LOG_BYTES = 500

# Default GGML model location (follows XDG-ish convention)
_DEFAULT_MODEL_PATH = Path.home() / ".cache" / "whisper-cpp" / "models" / "ggml-base.en.bin"

# Candidate binary names searched in PATH order
_BINARY_CANDIDATES = ("whisper-cpp", "whisper.cpp/main", "whisper")


class WhisperCppProvider:
    """STT provider using the Whisper.cpp binary (air-gap, no network).

    Satisfies the STTProvider Protocol via duck-typing.

    Attributes:
        _available: True when the binary is found on PATH at construction.
                    Used by skip-guards in tests and by VoiceModule health
                    checks.
    """

    def __init__(
        self,
        binary: str = "whisper-cpp",
        model_path: str | None = None,
        threads: int = 4,
        timeout_s: int = _DEFAULT_TIMEOUT_S,
    ) -> None:
        """Initialise the provider.

        Args:
            binary: Binary name or absolute path. Resolved against PATH if
                    it is not already absolute.
            model_path: Absolute path to the GGML model file.  ``None``
                        defers resolution to :meth:`_locate_model` at
                        transcription time (uses the default cache path).
            threads: Number of CPU threads passed to whisper.cpp.
            timeout_s: Maximum wall-clock seconds before the subprocess is
                       killed and ``STTFailed`` is raised.
        """
        self._binary_name = binary
        self._model_path_override = model_path
        self._threads = threads
        self._timeout_s = timeout_s

        # Resolve binary once at construction so we can expose _available.
        self._resolved_binary: str | None = self._locate_binary()
        self._available: bool = self._resolved_binary is not None

    # ------------------------------------------------------------------
    # Binary + model resolution
    # ------------------------------------------------------------------

    def _locate_binary(self) -> str | None:
        """Search for the whisper.cpp binary on PATH.

        Returns:
            Absolute binary path if found, else ``None``.

        Resolution order:
            1. Exact name passed as ``binary`` constructor arg.
            2. ``whisper-cpp`` (homebrew).
            3. ``whisper`` (some distro package names).
        """
        # Try the configured name first
        found = shutil.which(self._binary_name)
        if found:
            return found

        # Try well-known fallback names
        for candidate in _BINARY_CANDIDATES:
            found = shutil.which(candidate)
            if found:
                _logger.debug(
                    "whisper_cpp: configured binary '%s' not found; using '%s'",
                    self._binary_name,
                    candidate,
                )
                return found

        return None

    def _locate_model(self, model_path: str | None = None) -> Path:
        """Resolve and validate the GGML model file path.

        Args:
            model_path: Optional explicit path. If ``None``, falls back to
                        the constructor override, then the default cache
                        location.

        Returns:
            Resolved :class:`Path` if the model file exists.

        Raises:
            STTFailed: Model file not found at any candidate location.
        """
        # Priority: call-time arg → constructor override → default cache
        candidates: list[Path] = []

        if model_path:
            candidates.append(Path(model_path))
        if self._model_path_override:
            candidates.append(Path(self._model_path_override))
        candidates.append(_DEFAULT_MODEL_PATH)

        for candidate in candidates:
            if candidate.exists():
                return candidate

        searched = ", ".join(str(c) for c in candidates)
        raise STTFailed(
            f"Whisper.cpp model not found. Searched: {searched}. "
            "Download a model with: "
            "whisper.cpp/models/download-ggml-model.sh base.en  "
            "(or brew install whisper-cpp and run: "
            "whisper-cpp --model base.en --download-model)",
            details={"searched_paths": [str(c) for c in candidates]},
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
        model_path: str | None = None,
    ) -> TranscriptionResult:
        """Transcribe audio file via Whisper.cpp subprocess.

        Args:
            audio_path: Path to the audio file (WAV at 16kHz mono recommended;
                        other formats require ffmpeg pre-processing by the
                        caller).
            language: Optional BCP-47 language code for forced alignment
                      (e.g. ``"en"``, ``"de"``). ``None`` = auto-detect.
            model_path: Optional path override for the GGML model file,
                        takes precedence over the constructor default.

        Returns:
            :class:`TranscriptionResult` with transcribed text, detected
            language, audio duration, and optional per-token confidence
            averaged across segments.

        Raises:
            STTFailed: Binary absent, model missing, file unreadable,
                       subprocess non-zero exit, or timeout exceeded.
        """
        self._validate_audio_path(audio_path)

        if not self._available or self._resolved_binary is None:
            raise STTFailed(
                f"whisper-cpp binary '{self._binary_name}' not found on PATH. "
                "Install whisper.cpp: brew install whisper-cpp  "
                "or build from source at https://github.com/ggerganov/whisper.cpp",
                details={"binary": self._binary_name},
            )

        resolved_model = self._locate_model(model_path)
        cmd = self._build_command(self._resolved_binary, audio_path, resolved_model, language)
        _logger.debug("whisper_cpp: running %s", cmd)

        try:
            result = await self._run_subprocess(cmd)
        except TimeoutError as exc:
            raise STTFailed(
                f"whisper-cpp timed out after {self._timeout_s}s",
                details={"audio_path": str(audio_path), "timeout_s": self._timeout_s},
            ) from exc

        return self._parse_output(result, audio_path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_audio_path(self, audio_path: Path) -> None:
        """Raise STTFailed if the audio path is not usable."""
        if not audio_path.is_absolute():
            raise STTFailed(
                "audio_path must be absolute",
                details={"audio_path": str(audio_path)},
            )
        if not audio_path.exists():
            raise STTFailed(
                f"Audio file not found: {audio_path}",
                details={"audio_path": str(audio_path)},
            )

    def _build_command(
        self,
        binary_path: str,
        audio_path: Path,
        model: Path,
        language: str | None,
    ) -> list[str]:
        """Build the whisper-cpp command as a safe exec-style argument list.

        Uses ``-ojf`` (output JSON format) and ``-of -`` to emit JSON to
        stdout rather than writing a sidecar ``.json`` file.

        Args:
            binary_path: Resolved absolute path to the whisper-cpp binary.
            audio_path: Validated absolute path to the audio file.
            model: Validated path to the GGML model file.
            language: Optional BCP-47 language hint; omitted when ``None``.

        Returns:
            Argument list safe for :func:`asyncio.create_subprocess_exec`.
        """
        cmd = [
            binary_path,
            "-m",
            str(model),  # GGML model file
            "-f",
            str(audio_path),  # input audio
            "-t",
            str(self._threads),
            "-ojf",  # output JSON format
            "-of",
            "-",  # write output to stdout ("-" = stdout)
        ]
        if language:
            cmd += ["-l", language]
        return cmd

    async def _run_subprocess(self, cmd: list[str]) -> tuple[str, str, int]:
        """Spawn whisper-cpp and capture stdout/stderr.

        Args:
            cmd: Argument list for :func:`asyncio.create_subprocess_exec`.

        Returns:
            ``(stdout, stderr, returncode)`` tuple.

        Raises:
            TimeoutError: Process exceeded ``self._timeout_s`` seconds.
        """
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=self._timeout_s,
        )
        stdout = (stdout_bytes or b"").decode("utf-8", errors="replace")
        stderr = (stderr_bytes or b"").decode("utf-8", errors="replace")
        return stdout, stderr, proc.returncode or 0

    def _parse_output(
        self,
        result: tuple[str, str, int],
        audio_path: Path,
    ) -> TranscriptionResult:
        """Parse whisper-cpp JSON stdout into a :class:`TranscriptionResult`.

        Handles the ``-ojf`` JSON format::

            {"transcription": [{"text": "...", "offsets": {...}, "tokens": [...]}],
             "result": {"language": "en"}}

        Falls back to treating raw stdout as plain text when JSON parsing
        fails (some builds or flag combinations emit plain text).

        Args:
            result: ``(stdout, stderr, returncode)`` from :meth:`_run_subprocess`.
            audio_path: Used only in error messages.

        Returns:
            :class:`TranscriptionResult` with text, language, duration_s,
            and optional averaged confidence.

        Raises:
            STTFailed: Non-zero exit code or empty output after both parse
                       strategies.
        """
        stdout, stderr, returncode = result

        if returncode != 0:
            raise STTFailed(
                f"whisper-cpp exited with code {returncode}: {stderr[:_MAX_STDERR_LOG_BYTES]}",
                details={
                    "returncode": returncode,
                    "stderr": stderr[:_MAX_STDERR_LOG_BYTES],
                },
            )

        # Primary path: parse JSON from stdout
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            # Fallback: some builds emit plain text when -of - isn't supported
            _logger.debug("whisper_cpp: JSON parse failed; treating stdout as plain text")
            text = stdout.strip()
            if not text:
                raise STTFailed(
                    "whisper-cpp produced no output",
                    details={"audio_path": str(audio_path)},
                ) from None
            return TranscriptionResult(
                text=text,
                language="unknown",
                duration_s=0.0,
            )

        segments: list[dict[str, object]] = data.get("transcription", [])

        # Join segment texts — each typically has a leading space
        text = " ".join(str(seg.get("text", "")).strip() for seg in segments).strip()

        # Language lives under result.language in the JSON schema
        result_block: dict[str, object] = data.get("result", {})
        language: str = str(result_block.get("language", data.get("language", "unknown")))

        # Duration from last segment's "to" offset (milliseconds → seconds)
        duration_s = 0.0
        if segments:
            last = segments[-1]
            offsets: dict[str, object] = last.get("offsets", {})  # type: ignore[assignment]
            to_ms = offsets.get("to", 0)
            duration_s = float(to_ms) / 1000.0 if to_ms else 0.0  # type: ignore[arg-type]

        # Average per-token confidence when tokens carry probability scores
        confidence: float | None = self._extract_avg_confidence(segments)

        return TranscriptionResult(
            text=text,
            language=language,
            duration_s=duration_s,
            confidence=confidence,
        )

    @staticmethod
    def _extract_avg_confidence(
        segments: list[dict[str, object]],
    ) -> float | None:
        """Return average token probability across all segments, or None.

        Whisper.cpp tokens have a ``p`` field (0.0-1.0) in the JSON output
        when run with ``-ojf``.  If no tokens carry a ``p`` field, returns
        ``None`` so the caller knows confidence is unavailable.
        """
        probs: list[float] = []
        for seg in segments:
            tokens: list[dict[str, object]] = seg.get("tokens", [])  # type: ignore[assignment]
            for tok in tokens:
                p = tok.get("p")
                if p is not None:
                    probs.append(float(p))  # type: ignore[arg-type]
        if not probs:
            return None
        return sum(probs) / len(probs)
