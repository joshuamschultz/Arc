"""Tests for ElevenLabsProvider — mocked httpx calls."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.modules.voice.errors import TTSFailed
from arcagent.modules.voice.providers.elevenlabs import ElevenLabsProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider(**kwargs: Any) -> ElevenLabsProvider:
    defaults = {
        "api_key_env": "TEST_ELEVENLABS_KEY",
        "timeout_s": 10,
    }
    defaults.update(kwargs)
    return ElevenLabsProvider(**defaults)


def _fake_response(
    content: bytes = b"audio bytes",
    status_code: int = 200,
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = content
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


class TestElevenLabsProvider:
    @pytest.mark.asyncio
    async def test_synthesize_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        output = tmp_path / "out.mp3"
        monkeypatch.setenv("TEST_ELEVENLABS_KEY", "el-test-key")

        provider = _make_provider()
        fake_resp = _fake_response(content=b"mp3 audio bytes")

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=fake_resp)
        mock_client.aclose = AsyncMock()

        with patch("arcagent.modules.voice.providers.elevenlabs.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = mock_client
            result = await provider.synthesize("hello world", output_path=output)

        assert result == output
        assert output.read_bytes() == b"mp3 audio bytes"

    @pytest.mark.asyncio
    async def test_synthesize_with_custom_voice_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        output = tmp_path / "out.mp3"
        monkeypatch.setenv("TEST_ELEVENLABS_KEY", "el-test")

        provider = _make_provider()
        fake_resp = _fake_response()

        mock_client = AsyncMock()
        mock_client.aclose = AsyncMock()

        captured_url: list[str] = []

        async def capture_post(url: str, **kw: Any) -> Any:
            captured_url.append(url)
            return fake_resp

        mock_client.post = capture_post

        with patch("arcagent.modules.voice.providers.elevenlabs.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = mock_client
            await provider.synthesize(
                "test", voice_id="custom_voice_123", output_path=output
            )

        assert any("custom_voice_123" in u for u in captured_url)

    @pytest.mark.asyncio
    async def test_missing_api_key_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        output = tmp_path / "out.mp3"
        monkeypatch.delenv("TEST_ELEVENLABS_KEY", raising=False)

        provider = _make_provider()
        with pytest.raises(TTSFailed) as exc_info:
            await provider.synthesize("test", output_path=output)
        assert "TEST_ELEVENLABS_KEY" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_http_error_raises_tts_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        output = tmp_path / "out.mp3"
        monkeypatch.setenv("TEST_ELEVENLABS_KEY", "el-test")

        provider = _make_provider()
        fake_resp = _fake_response(status_code=401)

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=fake_resp)
        mock_client.aclose = AsyncMock()

        with patch("arcagent.modules.voice.providers.elevenlabs.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = mock_client
            with pytest.raises(TTSFailed):
                await provider.synthesize("test", output_path=output)

    @pytest.mark.asyncio
    async def test_timeout_raises_tts_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        output = tmp_path / "out.mp3"
        monkeypatch.setenv("TEST_ELEVENLABS_KEY", "el-test")

        provider = _make_provider()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=httpx.TimeoutException("timeout", request=MagicMock())
        )
        mock_client.aclose = AsyncMock()

        with patch("arcagent.modules.voice.providers.elevenlabs.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = mock_client
            with pytest.raises(TTSFailed) as exc_info:
                await provider.synthesize("test", output_path=output)
        assert "timed out" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_empty_response_raises_tts_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        output = tmp_path / "out.mp3"
        monkeypatch.setenv("TEST_ELEVENLABS_KEY", "el-test")

        provider = _make_provider()
        fake_resp = _fake_response(content=b"")  # empty body

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=fake_resp)
        mock_client.aclose = AsyncMock()

        with patch("arcagent.modules.voice.providers.elevenlabs.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = mock_client
            with pytest.raises(TTSFailed) as exc_info:
                await provider.synthesize("test", output_path=output)
        assert "empty" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_non_absolute_path_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_ELEVENLABS_KEY", "el-test")
        provider = _make_provider()
        with pytest.raises(TTSFailed) as exc_info:
            await provider.synthesize("test", output_path=Path("relative/out.mp3"))
        assert "absolute" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_api_key_never_in_logs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        output = tmp_path / "out.mp3"
        monkeypatch.setenv("TEST_ELEVENLABS_KEY", "el-super-secret-12345")

        provider = _make_provider()
        fake_resp = _fake_response()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=fake_resp)
        mock_client.aclose = AsyncMock()

        with caplog.at_level(logging.DEBUG, logger="arcagent.modules.voice"):
            with patch(
                "arcagent.modules.voice.providers.elevenlabs.httpx.AsyncClient"
            ) as mock_cls:
                mock_cls.return_value = mock_client
                await provider.synthesize("test", output_path=output)

        assert "el-super-secret-12345" not in caplog.text

    @pytest.mark.asyncio
    async def test_text_hash_logged_not_raw_text(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        output = tmp_path / "out.mp3"
        monkeypatch.setenv("TEST_ELEVENLABS_KEY", "el-test")

        provider = _make_provider()
        fake_resp = _fake_response()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=fake_resp)
        mock_client.aclose = AsyncMock()

        secret_text = "super-secret-synthesis-text-xyz"
        with caplog.at_level(logging.DEBUG, logger="arcagent.modules.voice"):
            with patch(
                "arcagent.modules.voice.providers.elevenlabs.httpx.AsyncClient"
            ) as mock_cls:
                mock_cls.return_value = mock_client
                await provider.synthesize(secret_text, output_path=output)

        # Raw text must NOT appear in logs
        assert secret_text not in caplog.text

    @pytest.mark.asyncio
    async def test_client_reuse_across_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """httpx.AsyncClient constructor must be called at most once (SPEC-018 Wave B1)."""
        monkeypatch.setenv("TEST_ELEVENLABS_KEY", "el-test")
        output1 = tmp_path / "out1.mp3"
        output2 = tmp_path / "out2.mp3"

        provider = _make_provider()
        fake_resp = _fake_response(content=b"audio")

        constructor_call_count = 0
        shared_client = AsyncMock()
        shared_client.post = AsyncMock(return_value=fake_resp)
        shared_client.aclose = AsyncMock()

        def counting_constructor(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal constructor_call_count
            constructor_call_count += 1
            return shared_client

        with patch(
            "arcagent.modules.voice.providers.elevenlabs.httpx.AsyncClient",
            counting_constructor,
        ):
            await provider.synthesize("hello", output_path=output1)
            await provider.synthesize("world", output_path=output2)

        assert constructor_call_count == 1, (
            f"httpx.AsyncClient was constructed {constructor_call_count} times; expected 1"
        )
