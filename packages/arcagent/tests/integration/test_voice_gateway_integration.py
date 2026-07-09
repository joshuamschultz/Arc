"""Integration test: voice-memo → transcribed → processed as text.

Simulates the G4.5 deliverable:
    A voice-memo message arrives (e.g. from Telegram/Discord adapter),
    the adapter downloads the audio attachment → the voice capability
    transcribes it → transcript is fed as text input to the agent pipeline.

Drives the production path: :mod:`arcagent.modules.voice._runtime` is
configured once, then :func:`arcagent.modules.voice.capabilities._transcribe`
runs the pipeline the loaded ``transcribe`` tool and hook both call.

The test mocks:
    - The STT provider (WhisperApiProvider) via httpx
    - The arcgateway adapter (a minimal fake PlatformAdapter)
    - The arcagent agent pipeline (a minimal fake that captures the text input)

No real network calls, no real binaries required.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.modules.voice import _runtime
from arcagent.modules.voice.capabilities import _transcribe
from arcagent.modules.voice.errors import AirGapProviderRequired
from arcagent.modules.voice.protocols import TranscriptionResult


@pytest.fixture(autouse=True)
def _reset_runtime() -> Iterator[None]:
    _runtime.reset()
    yield
    _runtime.reset()


# ---------------------------------------------------------------------------
# Fake adapter — simulates a platform adapter (Telegram-like) that receives
# a voice memo and hands the audio bytes to the voice module.
# ---------------------------------------------------------------------------


class FakePlatformAdapter:
    """Minimal platform adapter that downloads voice memos to tmp files."""

    def __init__(self, audio_bytes: bytes) -> None:
        self._audio_bytes = audio_bytes
        self.last_sent: list[str] = []

    async def download_voice_memo(self, *, dest: Path) -> Path:
        """Write fake audio bytes to dest and return the path."""
        dest.write_bytes(self._audio_bytes)
        return dest

    async def send_text(self, text: str) -> None:
        """Capture sent text for assertion."""
        self.last_sent.append(text)


# ---------------------------------------------------------------------------
# Fake agent pipeline — captures text passed to it.
# ---------------------------------------------------------------------------


class FakeAgent:
    """Minimal agent that records text inputs."""

    def __init__(self) -> None:
        self.received_inputs: list[str] = []

    async def process_text(self, text: str) -> str:
        self.received_inputs.append(text)
        return f"Agent processed: {text}"


# ---------------------------------------------------------------------------
# Voice-memo pipeline helper — ties adapter + voice runtime + agent together.
# ---------------------------------------------------------------------------


async def handle_voice_memo(
    *,
    adapter: FakePlatformAdapter,
    agent: FakeAgent,
    tmp_path: Path,
) -> str:
    """Simulate the full voice-memo → agent pipeline.

    1. Adapter downloads audio attachment.
    2. Voice capability transcribes audio to text.
    3. Transcript is fed to the agent as a text message.
    4. Agent response is sent back via adapter.
    """
    audio_path = tmp_path / "voice_memo.ogg"
    await adapter.download_voice_memo(dest=audio_path)

    result = await _transcribe(audio_path)
    transcript = result["text"]

    response = await agent.process_text(transcript)
    await adapter.send_text(response)

    return transcript


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestVoiceMemoToAgentIntegration:
    @pytest.mark.asyncio
    async def test_voice_memo_transcribed_and_processed(self, tmp_path: Path) -> None:
        """Happy-path: voice memo → transcribed → agent receives text."""
        audio_bytes = b"fake ogg voice data"
        adapter = FakePlatformAdapter(audio_bytes=audio_bytes)
        agent = FakeAgent()

        expected_transcript = "Please schedule a meeting for tomorrow at 3pm"
        mock_stt = AsyncMock()
        mock_stt.transcribe = AsyncMock(
            return_value=TranscriptionResult(
                text=expected_transcript, language="en", duration_s=4.2
            )
        )

        _runtime.configure(config={"tier": "personal", "stt_provider": "whisper_cpp"})
        _runtime.state().stt = mock_stt

        transcript = await handle_voice_memo(adapter=adapter, agent=agent, tmp_path=tmp_path)

        assert transcript == expected_transcript
        assert agent.received_inputs == [expected_transcript]
        assert len(adapter.last_sent) == 1
        assert "Agent processed" in adapter.last_sent[0]

    @pytest.mark.asyncio
    async def test_voice_memo_pii_redacted_enterprise(self, tmp_path: Path) -> None:
        """Enterprise tier: PII in voice memo is redacted before agent sees it."""
        adapter = FakePlatformAdapter(audio_bytes=b"voice data")
        agent = FakeAgent()

        raw_transcript = "My SSN is 123-45-6789 please update my records"
        mock_stt = AsyncMock()
        mock_stt.transcribe = AsyncMock(
            return_value=TranscriptionResult(text=raw_transcript, language="en", duration_s=3.0)
        )

        _runtime.configure(config={"tier": "enterprise", "stt_provider": "whisper_cpp"})
        _runtime.state().stt = mock_stt

        transcript = await handle_voice_memo(adapter=adapter, agent=agent, tmp_path=tmp_path)

        assert "123-45-6789" not in transcript
        assert "[SSN]" in transcript
        assert agent.received_inputs[0] == transcript

    @pytest.mark.asyncio
    async def test_voice_memo_audit_event_emitted(self, tmp_path: Path) -> None:
        """Transcription must emit voice.transcribed audit event."""
        adapter = FakePlatformAdapter(audio_bytes=b"voice data")
        agent = FakeAgent()

        mock_stt = AsyncMock()
        mock_stt.transcribe = AsyncMock(
            return_value=TranscriptionResult(
                text="secret voice content", language="en", duration_s=2.1
            )
        )

        mock_telemetry = MagicMock()
        _runtime.configure(config={"tier": "personal"}, telemetry=mock_telemetry)
        _runtime.state().stt = mock_stt

        await handle_voice_memo(adapter=adapter, agent=agent, tmp_path=tmp_path)

        transcribed_calls = [
            c
            for c in mock_telemetry.audit_event.call_args_list
            if c.args[0] == "voice.transcribed"
        ]
        assert len(transcribed_calls) == 1
        payload = transcribed_calls[0].args[1]

        assert "transcript_hash" in payload
        assert "secret voice content" not in str(payload)
        assert "duration_s" in payload
        assert "language" in payload
        assert "provider" in payload

    @pytest.mark.asyncio
    async def test_voice_memo_with_whisper_api_mock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Full pipeline using mocked WhisperApiProvider (httpx mocked)."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")

        adapter = FakePlatformAdapter(audio_bytes=b"fake audio bytes here")
        agent = FakeAgent()

        fake_api_response = MagicMock()
        fake_api_response.status_code = 200
        fake_api_response.json.return_value = {
            "text": "transcribed from whisper api",
            "language": "en",
            "duration": 2.0,
        }
        fake_api_response.raise_for_status.return_value = None

        mock_http_client = AsyncMock()
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=False)
        mock_http_client.post = AsyncMock(return_value=fake_api_response)

        with patch("arcagent.modules.voice.providers.whisper_api.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value = mock_http_client

            _runtime.configure(config={"tier": "personal", "stt_provider": "whisper_api"})
            transcript = await handle_voice_memo(
                adapter=adapter, agent=agent, tmp_path=tmp_path
            )

        assert transcript == "transcribed from whisper api"
        assert agent.received_inputs == ["transcribed from whisper api"]

    @pytest.mark.asyncio
    async def test_federal_cloud_provider_blocked_at_configure(self) -> None:
        """Federal tier must refuse to configure with cloud STT — fail before audio processed."""
        with pytest.raises(AirGapProviderRequired) as exc_info:
            _runtime.configure(
                config={
                    "tier": "federal",
                    "stt_provider": "whisper_api",
                    "tts_provider": "piper",
                }
            )
        assert exc_info.value.code == "VOICE_AIRGAP_REQUIRED"

    @pytest.mark.asyncio
    async def test_federal_airgap_module_accepts_voice_memo(self, tmp_path: Path) -> None:
        """Federal tier with air-gap providers processes voice memo correctly."""
        adapter = FakePlatformAdapter(audio_bytes=b"voice data")
        agent = FakeAgent()

        mock_stt = AsyncMock()
        mock_stt.transcribe = AsyncMock(
            return_value=TranscriptionResult(
                text="classified briefing notes", language="en", duration_s=10.0
            )
        )

        _runtime.configure(
            config={"tier": "federal", "stt_provider": "whisper_cpp", "tts_provider": "piper"}
        )
        _runtime.state().stt = mock_stt

        transcript = await handle_voice_memo(adapter=adapter, agent=agent, tmp_path=tmp_path)

        assert transcript == "classified briefing notes"
        assert agent.received_inputs[0] == transcript

    @pytest.mark.asyncio
    async def test_voice_memo_multiple_pii_types_redacted(self, tmp_path: Path) -> None:
        """Multiple PII types in a single voice memo are all redacted."""
        adapter = FakePlatformAdapter(audio_bytes=b"voice data")
        agent = FakeAgent()

        pii_heavy = (
            "Contact alice@example.com or call 555-867-5309. "
            "Card ending in 4111111111111111. SSN 123-45-6789."
        )
        mock_stt = AsyncMock()
        mock_stt.transcribe = AsyncMock(
            return_value=TranscriptionResult(text=pii_heavy, language="en", duration_s=5.0)
        )

        _runtime.configure(config={"tier": "enterprise", "stt_provider": "whisper_cpp"})
        _runtime.state().stt = mock_stt

        transcript = await handle_voice_memo(adapter=adapter, agent=agent, tmp_path=tmp_path)

        assert "alice@example.com" not in transcript
        assert "555-867-5309" not in transcript
        assert "123-45-6789" not in transcript
        assert "[EMAIL]" in transcript
        assert "[PHONE]" in transcript
        assert "[SSN]" in transcript

    @pytest.mark.asyncio
    async def test_voice_memo_language_detection_propagated(self, tmp_path: Path) -> None:
        """Detected language from STT is propagated to the result."""
        mock_stt = AsyncMock()
        mock_stt.transcribe = AsyncMock(
            return_value=TranscriptionResult(
                text="Bonjour le monde", language="fr", duration_s=1.5
            )
        )

        _runtime.configure(config={"tier": "personal"})
        _runtime.state().stt = mock_stt

        result = await _transcribe(tmp_path / "audio.ogg")
        assert result["language"] == "fr"

    @pytest.mark.asyncio
    async def test_adapter_download_creates_file(self, tmp_path: Path) -> None:
        """FakePlatformAdapter correctly writes audio bytes to destination."""
        audio_bytes = b"test audio content bytes"
        adapter = FakePlatformAdapter(audio_bytes=audio_bytes)
        dest = tmp_path / "memo.ogg"
        result_path = await adapter.download_voice_memo(dest=dest)
        assert result_path == dest
        assert dest.read_bytes() == audio_bytes
