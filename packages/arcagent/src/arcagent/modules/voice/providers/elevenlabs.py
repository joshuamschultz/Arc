"""ElevenLabs TTS provider — cloud, requires credentials.

Uses httpx (async) directly rather than the ElevenLabs SDK to keep the
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
    - Text sent over TLS-only endpoint.
    - Audio bytes written to output_path without logging content.
    - Only text_hash logged in audit events (never raw text).
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

import httpx

from arcagent.modules.voice.errors import TTSFailed

_logger = logging.getLogger("arcagent.modules.voice.providers.elevenlabs")

_ELEVENLABS_BASE_URL = "https://api.elevenlabs.io/v1"
_DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # Rachel
_DEFAULT_TIMEOUT_S = 30

# ElevenLabs Turbo v2 — low latency, high quality
_DEFAULT_MODEL_ID = "eleven_turbo_v2"


class ElevenLabsProvider:
    """TTS provider using the ElevenLabs REST API.

    Satisfies the TTSProvider Protocol via duck-typing.
    """

    def __init__(
        self,
        api_key_env: str = "ELEVENLABS_API_KEY",
        base_url: str = _ELEVENLABS_BASE_URL,
        default_voice_id: str = _DEFAULT_VOICE_ID,
        model_id: str = _DEFAULT_MODEL_ID,
        timeout_s: int = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._api_key_env = api_key_env
        self._base_url = base_url.rstrip("/")
        self._default_voice_id = default_voice_id
        self._model_id = model_id
        self._timeout_s = timeout_s
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

    async def synthesize(
        self,
        text: str,
        *,
        voice_id: str | None = None,
        output_path: Path,
    ) -> Path:
        """Synthesize text to an audio file via ElevenLabs API.

        Args:
            text: Plaintext to synthesize (must be PII-redacted upstream
                  at federal/enterprise tiers).
            voice_id: ElevenLabs voice ID. Uses the configured default
                      if not provided.
            output_path: Destination path for MP3 audio output.

        Returns:
            The same ``output_path`` after the file has been written.

        Raises:
            TTSFailed: API key missing, HTTP error, or empty response.
        """
        api_key = self._get_api_key()
        self._validate_output_path(output_path)

        effective_voice = voice_id or self._default_voice_id

        # Log text hash for audit trail — NEVER log raw text
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        _logger.debug(
            "elevenlabs: synthesizing voice_id=%s text_hash=%s",
            effective_voice,
            text_hash[:16],
        )

        try:
            audio_bytes = await self._call_api(api_key, effective_voice, text)
        except httpx.TimeoutException as exc:
            raise TTSFailed(
                f"ElevenLabs API timed out after {self._timeout_s}s",
                details={"timeout_s": self._timeout_s},
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise TTSFailed(
                f"ElevenLabs API returned HTTP {exc.response.status_code}",
                details={"status_code": exc.response.status_code},
            ) from exc

        if not audio_bytes:
            raise TTSFailed(
                "ElevenLabs returned empty audio response",
                details={"voice_id": effective_voice},
            )

        output_path.write_bytes(audio_bytes)
        return output_path

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_api_key(self) -> str:
        """Read API key from environment; raise TTSFailed if absent."""
        key = os.environ.get(self._api_key_env, "").strip()
        if not key:
            raise TTSFailed(
                f"ElevenLabs API key not found in env var '{self._api_key_env}'",
                details={"env_var": self._api_key_env},
            )
        return key

    def _validate_output_path(self, output_path: Path) -> None:
        """Raise TTSFailed if output_path is not usable."""
        if not output_path.is_absolute():
            raise TTSFailed(
                "output_path must be absolute",
                details={"output_path": str(output_path)},
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)

    async def _call_api(
        self,
        api_key: str,
        voice_id: str,
        text: str,
    ) -> bytes:
        """POST to ElevenLabs TTS endpoint and return audio bytes."""
        url = f"{self._base_url}/text-to-speech/{voice_id}"
        payload = {
            "text": text,
            "model_id": self._model_id,
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
            },
        }

        client = self._get_client()
        resp = await client.post(
            url,
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json=payload,
        )
        resp.raise_for_status()
        return resp.content
