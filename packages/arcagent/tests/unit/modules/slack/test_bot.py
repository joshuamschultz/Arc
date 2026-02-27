"""Unit tests for SlackBot — SPEC-011 Phase 2."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.modules.slack.bot import SlackBot, split_message
from arcagent.modules.slack.config import SlackConfig

# ── split_message tests ─────────────────────────────────────────


class TestSplitMessage:
    def test_short_text_no_split(self) -> None:
        result = split_message("Hello world")
        assert result == ["Hello world"]

    def test_empty_text(self) -> None:
        result = split_message("")
        assert result == []

    def test_exactly_max_length(self) -> None:
        text = "a" * 4000
        result = split_message(text)
        assert result == [text]

    def test_split_at_paragraph_boundary(self) -> None:
        """Double-newline is highest priority split point."""
        part1 = "a" * 3000
        part2 = "b" * 3000
        text = part1 + "\n\n" + part2
        result = split_message(text)
        assert len(result) == 2
        assert result[0] == part1
        assert result[1] == part2

    def test_split_at_single_newline(self) -> None:
        """Single newline is second priority when no paragraph breaks fit."""
        part1 = "a" * 3000
        part2 = "b" * 3000
        text = part1 + "\n" + part2
        result = split_message(text)
        assert len(result) == 2
        assert result[0] == part1
        assert result[1] == part2

    def test_split_at_sentence_boundary(self) -> None:
        """Sentence boundary (. ! ?) is third priority."""
        part1 = "a" * 3000 + "."
        part2 = " " + "b" * 3000
        text = part1 + part2
        result = split_message(text)
        assert len(result) == 2
        assert result[0] == part1
        assert result[1] == part2.lstrip()

    def test_hard_split_no_boundaries(self) -> None:
        """Falls back to hard split at max_length."""
        text = "a" * 5000
        result = split_message(text, max_length=4000)
        assert len(result) == 2
        assert len(result[0]) == 4000
        assert len(result[1]) == 1000

    def test_multi_split(self) -> None:
        """Text > 2x max_length produces 3+ chunks."""
        text = "a" * 10000
        result = split_message(text, max_length=4000)
        assert len(result) == 3
        assert all(len(chunk) <= 4000 for chunk in result)
        assert "".join(result) == text

    def test_custom_max_length(self) -> None:
        text = "Hello world, this is a test."
        result = split_message(text, max_length=10)
        assert all(len(chunk) <= 10 for chunk in result)
        assert "".join(result) == text

    def test_preserves_all_content(self) -> None:
        """No content should be lost during splitting."""
        text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
        result = split_message(text, max_length=30)
        assert all(chunk in text for chunk in result)


# ── Fixtures ─────────────────────────────────────────────────────


def _make_config(**overrides: object) -> SlackConfig:
    defaults: dict[str, object] = {"enabled": True, "allowed_user_ids": ["U123"]}
    defaults.update(overrides)
    return SlackConfig(**defaults)


def _make_bot(tmp_path: Path, **config_overrides: object) -> SlackBot:
    config = _make_config(**config_overrides)
    telemetry = MagicMock()
    telemetry.record_event = MagicMock()
    return SlackBot(config=config, telemetry=telemetry, workspace=tmp_path)


def _attach_mock_app(bot: SlackBot) -> MagicMock:
    """Attach a mock Slack app with async client methods to a bot."""
    app = MagicMock()
    app.client = MagicMock()
    app.client.chat_postMessage = AsyncMock()
    app.client.conversations_open = AsyncMock(return_value={"channel": {"id": "D99999"}})
    bot._app = app
    return app


# ── SlackBot Construction ──────────────────────────────────────


class TestBotConstruction:
    def test_creates_with_config(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        assert bot._config.enabled is True
        assert bot._config.allowed_user_ids == ["U123"]

    def test_starts_with_no_session(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        assert bot._user_id is None
        assert bot._current_session_id is None

    def test_loads_persisted_state(self, tmp_path: Path) -> None:
        """Bot loads user_id and session_id from state.json."""
        state_dir = tmp_path / "slack"
        state_dir.mkdir(parents=True)
        state_path = state_dir / "state.json"
        state_path.write_text(
            json.dumps(
                {
                    "user_id": "U999",
                    "session_id": "abc-123",
                    "dm_channel_id": "D12345",
                }
            )
        )

        bot = _make_bot(tmp_path)
        assert bot._user_id == "U999"
        assert bot._current_session_id == "abc-123"
        assert bot._dm_channel_id == "D12345"


# ── Authorization ──────────────────────────────────────────────


class TestBotAuthorization:
    def test_authorized_user_id(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path, allowed_user_ids=["U111", "U222"])
        assert bot._is_authorized("U111") is True
        assert bot._is_authorized("U222") is True

    def test_unauthorized_user_id(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path, allowed_user_ids=["U111"])
        assert bot._is_authorized("U999") is False

    def test_empty_allowlist_denies_all(self, tmp_path: Path) -> None:
        """Empty allowed_user_ids = fail-closed (deny all)."""
        bot = _make_bot(tmp_path, allowed_user_ids=[])
        assert bot._is_authorized("U123") is False


# ── Message Handler ────────────────────────────────────────────


class TestHandleMessage:
    @pytest.mark.asyncio
    async def test_authorized_message_calls_agent_chat(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        bot._current_session_id = "test-session"
        app = _attach_mock_app(bot)

        mock_chat = AsyncMock(return_value=MagicMock(content="Hello back"))
        bot.set_agent_chat_fn(mock_chat)

        event = {"user": "U123", "channel": "D12345", "text": "hello"}
        await bot._handle_message(event)

        mock_chat.assert_called_once_with("hello", session_id="test-session")
        app.client.chat_postMessage.assert_called_once_with(channel="D12345", text="Hello back")

    @pytest.mark.asyncio
    async def test_unauthorized_user_silently_ignored(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path, allowed_user_ids=["U111"])
        app = _attach_mock_app(bot)

        mock_chat = AsyncMock()
        bot.set_agent_chat_fn(mock_chat)

        event = {"user": "U999", "channel": "D12345", "text": "sneaky"}
        await bot._handle_message(event)

        mock_chat.assert_not_called()
        app.client.chat_postMessage.assert_not_called()

    @pytest.mark.asyncio
    async def test_unauthorized_emits_auth_rejected(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path, allowed_user_ids=["U111"])

        event = {"user": "U999", "channel": "D12345", "text": "sneaky"}
        await bot._handle_message(event)

        calls = bot._telemetry.record_event.call_args_list
        event_names = [c[0][0] for c in calls]
        assert "slack:auth_rejected" in event_names

    @pytest.mark.asyncio
    async def test_bot_message_skipped(self, tmp_path: Path) -> None:
        """Messages with bot_id are skipped (prevent infinite loops)."""
        bot = _make_bot(tmp_path)
        mock_chat = AsyncMock()
        bot.set_agent_chat_fn(mock_chat)

        event = {"user": "U123", "channel": "D12345", "text": "hello", "bot_id": "B123"}
        await bot._handle_message(event)

        mock_chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_session_if_none(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        _attach_mock_app(bot)

        mock_chat = AsyncMock(return_value=MagicMock(content="hi"))
        bot.set_agent_chat_fn(mock_chat)

        assert bot._current_session_id is None
        event = {"user": "U123", "channel": "D12345", "text": "hello"}
        await bot._handle_message(event)

        assert bot._current_session_id is not None

    @pytest.mark.asyncio
    async def test_strips_bot_mentions(self, tmp_path: Path) -> None:
        """Bot mentions like <@U123BOTID> are stripped from text."""
        bot = _make_bot(tmp_path)
        bot._current_session_id = "test-session"
        _attach_mock_app(bot)

        mock_chat = AsyncMock(return_value=MagicMock(content="reply"))
        bot.set_agent_chat_fn(mock_chat)

        event = {"user": "U123", "channel": "D12345", "text": "<@U999BOT> hello"}
        await bot._handle_message(event)

        mock_chat.assert_called_once_with("hello", session_id="test-session")

    @pytest.mark.asyncio
    async def test_empty_text_skipped(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        mock_chat = AsyncMock()
        bot.set_agent_chat_fn(mock_chat)

        event = {"user": "U123", "channel": "D12345", "text": ""}
        await bot._handle_message(event)

        mock_chat.assert_not_called()


# ── Text Command Handlers ──────────────────────────────────────


class TestTextCommands:
    @pytest.mark.asyncio
    async def test_start_creates_session(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        app = _attach_mock_app(bot)

        event = {"user": "U123", "channel": "D12345", "text": "start"}
        await bot._handle_message(event)

        assert bot._user_id == "U123"
        assert bot._current_session_id is not None
        app.client.chat_postMessage.assert_called_once()
        msg = app.client.chat_postMessage.call_args[1]["text"]
        assert "Connected" in msg

    @pytest.mark.asyncio
    async def test_new_rotates_session(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        app = _attach_mock_app(bot)

        # First set up a session
        event = {"user": "U123", "channel": "D12345", "text": "start"}
        await bot._handle_message(event)
        old_session = bot._current_session_id

        # Now rotate
        app.client.chat_postMessage.reset_mock()
        event = {"user": "U123", "channel": "D12345", "text": "new"}
        await bot._handle_message(event)

        assert bot._current_session_id != old_session
        assert bot._current_session_id is not None
        msg = app.client.chat_postMessage.call_args[1]["text"]
        assert "New session" in msg

    @pytest.mark.asyncio
    async def test_status_returns_info(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        app = _attach_mock_app(bot)

        # Set up session first
        event = {"user": "U123", "channel": "D12345", "text": "start"}
        await bot._handle_message(event)

        app.client.chat_postMessage.reset_mock()
        event = {"user": "U123", "channel": "D12345", "text": "status"}
        await bot._handle_message(event)

        msg = app.client.chat_postMessage.call_args[1]["text"]
        assert "Session:" in msg
        assert "User:" in msg
        assert "Connected:" in msg


# ── Process Message ────────────────────────────────────────────


class TestProcessMessage:
    @pytest.mark.asyncio
    async def test_splits_long_response(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path, max_message_length=20)
        bot._current_session_id = "test-session"
        app = _attach_mock_app(bot)

        long_text = "a" * 50
        mock_chat = AsyncMock(return_value=MagicMock(content=long_text))
        bot.set_agent_chat_fn(mock_chat)

        await bot._process_message("hi", "D12345")

        assert app.client.chat_postMessage.call_count == 3

    @pytest.mark.asyncio
    async def test_handles_no_response(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        bot._current_session_id = "test-session"
        app = _attach_mock_app(bot)

        mock_chat = AsyncMock(return_value=None)
        bot.set_agent_chat_fn(mock_chat)

        await bot._process_message("hi", "D12345")

        app.client.chat_postMessage.assert_called_once_with(channel="D12345", text="(No response)")

    @pytest.mark.asyncio
    async def test_skips_if_no_chat_fn(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        app = _attach_mock_app(bot)

        await bot._process_message("hi", "D12345")

        app.client.chat_postMessage.assert_called_once_with(
            channel="D12345",
            text="Agent not ready — chat function not bound.",
        )

    @pytest.mark.asyncio
    async def test_emits_message_sent_event(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        bot._current_session_id = "test-session"
        _attach_mock_app(bot)

        mock_chat = AsyncMock(return_value=MagicMock(content="reply"))
        bot.set_agent_chat_fn(mock_chat)

        await bot._process_message("hi", "D12345")

        calls = bot._telemetry.record_event.call_args_list
        event_names = [c[0][0] for c in calls]
        assert "slack:message_sent" in event_names

    @pytest.mark.asyncio
    async def test_handles_agent_chat_error(self, tmp_path: Path) -> None:
        """agent.chat() error sends error message to user."""
        bot = _make_bot(tmp_path)
        bot._current_session_id = "test-session"
        app = _attach_mock_app(bot)

        mock_chat = AsyncMock(side_effect=RuntimeError("LLM error"))
        bot.set_agent_chat_fn(mock_chat)

        await bot._process_message("hi", "D12345")

        app.client.chat_postMessage.assert_called_once_with(
            channel="D12345",
            text="Error processing your message. Please try again.",
        )

    async def test_rate_limit_error_shows_specific_message(self, tmp_path: Path) -> None:
        """429 errors tell the user they're rate limited."""
        from arcllm.exceptions import ArcLLMAPIError

        bot = _make_bot(tmp_path)
        bot._current_session_id = "test-session"
        app = _attach_mock_app(bot)

        error = ArcLLMAPIError(status_code=429, body="rate limited", provider="azure")
        mock_chat = AsyncMock(side_effect=error)
        bot.set_agent_chat_fn(mock_chat)

        await bot._process_message("hi", "D12345")

        app.client.chat_postMessage.assert_called_once_with(
            channel="D12345",
            text="I'm currently rate limited by the LLM provider. Please try again in a minute or two.",
        )

    async def test_server_error_shows_specific_message(self, tmp_path: Path) -> None:
        """500/502/503 errors tell the user the provider is unavailable."""
        from arcllm.exceptions import ArcLLMAPIError

        bot = _make_bot(tmp_path)
        bot._current_session_id = "test-session"
        app = _attach_mock_app(bot)

        error = ArcLLMAPIError(status_code=502, body="bad gateway", provider="azure")
        mock_chat = AsyncMock(side_effect=error)
        bot.set_agent_chat_fn(mock_chat)

        await bot._process_message("hi", "D12345")

        app.client.chat_postMessage.assert_called_once_with(
            channel="D12345",
            text="The LLM provider is temporarily unavailable. Please try again shortly.",
        )


# ── Send Notification ──────────────────────────────────────────


class TestSendNotification:
    @pytest.mark.asyncio
    async def test_sends_to_stored_user(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        bot._user_id = "U123"
        bot._dm_channel_id = "D12345"
        app = _attach_mock_app(bot)

        await bot.send_notification("Task completed!")

        app.client.chat_postMessage.assert_called_once_with(
            channel="D12345", text="Task completed!"
        )

    @pytest.mark.asyncio
    async def test_opens_dm_channel_if_not_cached(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        bot._user_id = "U123"
        bot._dm_channel_id = None  # Not cached
        app = _attach_mock_app(bot)

        await bot.send_notification("hello")

        app.client.conversations_open.assert_called_once_with(users="U123")
        app.client.chat_postMessage.assert_called_once_with(channel="D99999", text="hello")
        # Channel should now be cached
        assert bot._dm_channel_id == "D99999"

    @pytest.mark.asyncio
    async def test_skips_if_no_user_id(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        bot._user_id = None
        await bot.send_notification("hello")  # Should not raise

    @pytest.mark.asyncio
    async def test_skips_if_bot_not_running(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        bot._user_id = "U123"
        bot._app = None
        await bot.send_notification("hello")  # Should not raise

    @pytest.mark.asyncio
    async def test_emits_notification_event(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        bot._user_id = "U123"
        bot._dm_channel_id = "D12345"
        _attach_mock_app(bot)

        await bot.send_notification("Done!")

        calls = bot._telemetry.record_event.call_args_list
        event_names = [c[0][0] for c in calls]
        assert "slack:notification_sent" in event_names

    @pytest.mark.asyncio
    async def test_dm_channel_cached_after_first_call(self, tmp_path: Path) -> None:
        """conversations.open should only be called once, then cached."""
        bot = _make_bot(tmp_path)
        bot._user_id = "U123"
        bot._dm_channel_id = None
        app = _attach_mock_app(bot)

        await bot.send_notification("first")
        await bot.send_notification("second")

        # conversations_open called only once (cached after first)
        app.client.conversations_open.assert_called_once()


# ── Start / Token Handling ─────────────────────────────────────


class TestBotStart:
    @pytest.mark.asyncio
    async def test_stays_dormant_without_bot_token(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        with patch.dict("os.environ", {}, clear=True):
            await bot.start()

        assert bot._app is None
        assert bot._running is False

    @pytest.mark.asyncio
    async def test_stays_dormant_without_app_token(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        with patch.dict(
            "os.environ",
            {"ARCAGENT_SLACK_BOT_TOKEN": "xoxb-test"},
            clear=True,
        ):
            await bot.start()

        assert bot._app is None
        assert bot._running is False

    @pytest.mark.asyncio
    async def test_rejects_wrong_bot_token_prefix(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        with patch.dict(
            "os.environ",
            {
                "ARCAGENT_SLACK_BOT_TOKEN": "xapp-wrong-prefix",
                "ARCAGENT_SLACK_APP_TOKEN": "xapp-test",
            },
            clear=True,
        ):
            await bot.start()

        assert bot._app is None
        assert bot._running is False

    @pytest.mark.asyncio
    async def test_rejects_wrong_app_token_prefix(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        with patch.dict(
            "os.environ",
            {
                "ARCAGENT_SLACK_BOT_TOKEN": "xoxb-test",
                "ARCAGENT_SLACK_APP_TOKEN": "xoxb-wrong-prefix",
            },
            clear=True,
        ):
            await bot.start()

        assert bot._app is None
        assert bot._running is False


# ── State Persistence ──────────────────────────────────────────


class TestStatePersistence:
    def test_save_and_load(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        bot._user_id = "U456"
        bot._current_session_id = "sess-xyz"
        bot._dm_channel_id = "D789"
        bot._save_state()

        bot2 = _make_bot(tmp_path)
        assert bot2._user_id == "U456"
        assert bot2._current_session_id == "sess-xyz"
        assert bot2._dm_channel_id == "D789"

    def test_handles_corrupt_state(self, tmp_path: Path) -> None:
        """Corrupt state file should not crash — starts fresh."""
        state_dir = tmp_path / "slack"
        state_dir.mkdir(parents=True)
        (state_dir / "state.json").write_text("not valid json{{{")

        bot = _make_bot(tmp_path)
        assert bot._user_id is None
        assert bot._current_session_id is None

    def test_state_file_has_restricted_permissions(self, tmp_path: Path) -> None:
        """State file should be owner-only readable (0o600)."""
        bot = _make_bot(tmp_path)
        bot._user_id = "U123"
        bot._save_state()

        mode = bot._state_path.stat().st_mode & 0o777
        assert mode == 0o600
