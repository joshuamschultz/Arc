"""Tests for WhisperApiProvider — mocked httpx calls."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.modules.voice.errors import STTFailed
from arcagent.modules.voice.providers.whisper_api import WhisperApiProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider(**kwargs: Any) -> WhisperApiProvider:
    defaults = {
        "api_key_env": "TEST_OPENAI_KEY",
        "model": "whisper-1",
        "timeout_s": 10,
    }
    defaults.update(kwargs)
    return WhisperApiProvider(**defaults)


def _fake_response(
    text: str = "hello",
    language: str = "en",
    duration: float = 2.5,
    status_code: int = 200,
) -> MagicMock:
    """Build a fake httpx response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {
        "text": text,
        "language": language,
        "duration": duration,
    }
    if status_code != 200:
        import httpx

        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status_code}",
            request=MagicMock(),
            response=resp,
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWhisperApiProvider:
    @pytest.mark.asyncio
    async def test_transcribe_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        audio = tmp_path / "audio.mp3"
        audio.write_bytes(b"fake mp3 data")
        monkeypatch.setenv("TEST_OPENAI_KEY", "sk-test-key")

        provider = _make_provider()
        fake_resp = _fake_response(text="hello world", language="en", duration=3.0)

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=fake_resp)
        mock_client.aclose = AsyncMock()

        with patch("arcagent.modules.voice.providers.whisper_api.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = mock_client
            result = await provider.transcribe(audio)

        assert result.text == "hello world"
        assert result.language == "en"
        assert result.duration_s == pytest.approx(3.0)

    @pytest.mark.asyncio
    async def test_transcribe_with_language_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        audio = tmp_path / "audio.mp3"
        audio.write_bytes(b"data")
        monkeypatch.setenv("TEST_OPENAI_KEY", "sk-test")

        provider = _make_provider()
        fake_resp = _fake_response(text="hola", language="es", duration=1.0)

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=fake_resp)
        mock_client.aclose = AsyncMock()

        with patch("arcagent.modules.voice.providers.whisper_api.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = mock_client
            result = await provider.transcribe(audio, language="es")

        assert result.language == "es"

    @pytest.mark.asyncio
    async def test_missing_api_key_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        audio = tmp_path / "audio.mp3"
        audio.write_bytes(b"data")
        monkeypatch.delenv("TEST_OPENAI_KEY", raising=False)

        provider = _make_provider()
        with pytest.raises(STTFailed) as exc_info:
            await provider.transcribe(audio)
        assert "TEST_OPENAI_KEY" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_http_error_raises_stt_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        audio = tmp_path / "audio.mp3"
        audio.write_bytes(b"data")
        monkeypatch.setenv("TEST_OPENAI_KEY", "sk-test")

        provider = _make_provider()
        fake_resp = _fake_response(status_code=429)

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=fake_resp)
        mock_client.aclose = AsyncMock()

        with patch("arcagent.modules.voice.providers.whisper_api.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = mock_client
            with pytest.raises(STTFailed):
                await provider.transcribe(audio)

    @pytest.mark.asyncio
    async def test_timeout_raises_stt_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        audio = tmp_path / "audio.mp3"
        audio.write_bytes(b"data")
        monkeypatch.setenv("TEST_OPENAI_KEY", "sk-test")

        provider = _make_provider()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=httpx.TimeoutException("timeout", request=MagicMock())
        )
        mock_client.aclose = AsyncMock()

        with patch("arcagent.modules.voice.providers.whisper_api.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = mock_client
            with pytest.raises(STTFailed) as exc_info:
                await provider.transcribe(audio)
        assert "timed out" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_non_absolute_path_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_OPENAI_KEY", "sk-test")
        provider = _make_provider()
        with pytest.raises(STTFailed) as exc_info:
            await provider.transcribe(Path("relative/path.mp3"))
        assert "absolute" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_missing_file_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_OPENAI_KEY", "sk-test")
        provider = _make_provider()
        with pytest.raises(STTFailed) as exc_info:
            await provider.transcribe(tmp_path / "nonexistent.mp3")
        assert "not found" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_oversized_file_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        audio = tmp_path / "huge.mp3"
        # Write just over 25MB
        audio.write_bytes(b"x" * (26 * 1024 * 1024))
        monkeypatch.setenv("TEST_OPENAI_KEY", "sk-test")

        provider = _make_provider()
        with pytest.raises(STTFailed) as exc_info:
            await provider.transcribe(audio)
        assert "25MB" in str(exc_info.value) or "limit" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_api_key_never_in_debug_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """API key must not appear in log output."""
        import logging

        audio = tmp_path / "audio.mp3"
        audio.write_bytes(b"data")
        monkeypatch.setenv("TEST_OPENAI_KEY", "sk-super-secret-key-12345")

        provider = _make_provider()
        fake_resp = _fake_response()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=fake_resp)
        mock_client.aclose = AsyncMock()

        with caplog.at_level(logging.DEBUG, logger="arcagent.modules.voice"):
            with patch(
                "arcagent.modules.voice.providers.whisper_api.httpx.AsyncClient"
            ) as mock_cls:
                mock_cls.return_value = mock_client
                await provider.transcribe(audio)

        assert "sk-super-secret-key-12345" not in caplog.text

    @pytest.mark.asyncio
    async def test_client_reuse_across_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """httpx.AsyncClient constructor must be called at most once (SPEC-018 Wave B1)."""
        monkeypatch.setenv("TEST_OPENAI_KEY", "sk-test")
        audio = tmp_path / "audio.mp3"
        audio.write_bytes(b"fake data")

        provider = _make_provider()
        fake_resp = _fake_response(text="hello")

        constructor_call_count = 0
        shared_client = AsyncMock()
        shared_client.post = AsyncMock(return_value=fake_resp)
        shared_client.aclose = AsyncMock()

        def counting_constructor(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal constructor_call_count
            constructor_call_count += 1
            return shared_client

        with patch(
            "arcagent.modules.voice.providers.whisper_api.httpx.AsyncClient",
            counting_constructor,
        ):
            await provider.transcribe(audio)
            await provider.transcribe(audio)

        assert constructor_call_count == 1, (
            f"httpx.AsyncClient was constructed {constructor_call_count} times; expected 1"
        )
