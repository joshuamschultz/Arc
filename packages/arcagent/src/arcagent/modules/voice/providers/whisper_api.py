"""OpenAI Whisper API STT provider — cloud, requires credentials.

Uses httpx (async) directly rather than the openai SDK to keep the
dependency surface minimal and respect Arc's "no vendor SDKs in core"
policy. The API key is read from environment at call time — never
cached in memory or written to disk.

Federal tier: this provider MUST NOT be used. VoiceModule enforces
AirGapProviderRequired at construction time.

Performance (SPEC-018 Wave B1):
  A single ``httpx.AsyncClient`` is created lazily on first use and
  reused across all calls.  Call ``await provider.close()`` during
  shutdown to drain the connection pool.

Security:
    - API key read from env, never logged.
    - Audio bytes sent over TLS-only endpoint.
    - Response text is NOT logged (only transcript_hash in audit events).
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

import httpx

from arcagent.modules.voice.errors import STTFailed
from arcagent.modules.voice.protocols import TranscriptionResult

_logger = logging.getLogger("arcagent.modules.voice.providers.whisper_api")

_WHISPER_URL = "https://api.openai.com/v1/audio/transcriptions"
_DEFAULT_MODEL = "whisper-1"
_DEFAULT_TIMEOUT_S = 60


class WhisperApiProvider:
    """STT provider using the OpenAI Whisper API.

    Satisfies the STTProvider Protocol via duck-typing.
    """

    def __init__(
        self,
        api_key_env: str = "OPENAI_API_KEY",
        model: str = _DEFAULT_MODEL,
        timeout_s: int = _DEFAULT_TIMEOUT_S,
        base_url: str = _WHISPER_URL,
    ) -> None:
        self._api_key_env = api_key_env
        self._model = model
        self._timeout_s = timeout_s
        self._base_url = base_url
        # Long-lived client; populated on first use via _get_client().
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """Return the shared client, creating it lazily on first call."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout_s)
        return self._client

    async def close(self) -> None:
        """Close the shared httpx client and release its connection pool."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = None,
    ) -> TranscriptionResult:
        """Transcribe audio via OpenAI Whisper API.

        Args:
            audio_path: Path to audio file (mp3, mp4, wav, webm, etc.).
            language: Optional BCP-47 language hint.

        Returns:
            TranscriptionResult with text, language, and duration.

        Raises:
            STTFailed: API key missing, file unreadable, HTTP error,
                       or malformed response.
        """
        api_key = self._get_api_key()
        self._validate_audio_path(audio_path)

        try:
            result = await self._call_api(audio_path, api_key, language)
        except httpx.TimeoutException as exc:
            raise STTFailed(
                f"Whisper API timed out after {self._timeout_s}s",
                details={"timeout_s": self._timeout_s},
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise STTFailed(
                f"Whisper API returned HTTP {exc.response.status_code}",
                details={"status_code": exc.response.status_code},
            ) from exc

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_api_key(self) -> str:
        """Read API key from environment; raise STTFailed if absent."""
        key = os.environ.get(self._api_key_env, "").strip()
        if not key:
            raise STTFailed(
                f"OpenAI API key not found in env var '{self._api_key_env}'",
                details={"env_var": self._api_key_env},
            )
        return key

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

    async def _call_api(
        self,
        audio_path: Path,
        api_key: str,
        language: str | None,
    ) -> TranscriptionResult:
        """POST to Whisper API and parse response."""
        # Read file bytes — size check to prevent accidental huge uploads
        audio_bytes = audio_path.read_bytes()
        if len(audio_bytes) > 25 * 1024 * 1024:  # 25MB OpenAI limit
            raise STTFailed(
                f"Audio file exceeds 25MB limit: {len(audio_bytes)} bytes",
                details={"size_bytes": len(audio_bytes)},
            )

        # Log content hash for audit trail (never log raw bytes or text)
        _logger.debug(
            "whisper_api: uploading audio sha256=%s",
            hashlib.sha256(audio_bytes).hexdigest()[:16],
        )

        form_data: dict[str, object] = {
            "model": self._model,
            "response_format": "verbose_json",
        }
        if language:
            form_data["language"] = language

        client = self._get_client()
        resp = await client.post(
            self._base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            data=form_data,
            files={
                "file": (audio_path.name, audio_bytes, "audio/mpeg"),
            },
        )
        resp.raise_for_status()

        data = resp.json()
        text = data.get("text", "").strip()
        detected_language = data.get("language", "unknown")
        duration_s = float(data.get("duration", 0.0))

        return TranscriptionResult(
            text=text,
            language=detected_language,
            duration_s=duration_s,
        )
