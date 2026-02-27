"""Unit tests for TelegramBot — S005 Phase 2."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.modules.telegram.bot import TelegramBot, split_message
from arcagent.modules.telegram.config import TelegramConfig


class TestSplitMessage:
    def test_short_text_no_split(self) -> None:
        result = split_message("Hello world")
        assert result == ["Hello world"]

    def test_empty_text(self) -> None:
        result = split_message("")
        assert result == []

    def test_exactly_max_length(self) -> None:
        text = "a" * 4096
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
        result = split_message(text, max_length=4096)
        assert len(result) == 2
        assert len(result[0]) == 4096
        assert len(result[1]) == 904

    def test_multi_split(self) -> None:
        """Text > 2x max_length produces 3+ chunks."""
        text = "a" * 10000
        result = split_message(text, max_length=4096)
        assert len(result) == 3
        assert all(len(chunk) <= 4096 for chunk in result)
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
        # Rejoin with paragraph separators and verify nothing lost
        assert all(chunk in text for chunk in result)


# ── Fixtures ─────────────────────────────────────────────────────


def _make_config(**overrides: object) -> TelegramConfig:
    defaults = {"enabled": True, "allowed_chat_ids": [123]}
    defaults.update(overrides)
    return TelegramConfig(**defaults)


def _make_bot(tmp_path: Path, **config_overrides: object) -> TelegramBot:
    config = _make_config(**config_overrides)
    telemetry = MagicMock()
    telemetry.record_event = MagicMock()
    return TelegramBot(config=config, telemetry=telemetry, workspace=tmp_path)


def _make_update(chat_id: int = 123, text: str = "hello") -> MagicMock:
    """Create a mock Telegram Update object."""
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.send_action = AsyncMock()
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


# ── TelegramBot Construction ────────────────────────────────────


class TestBotConstruction:
    def test_creates_with_config(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        assert bot._config.enabled is True
        assert bot._config.allowed_chat_ids == [123]

    def test_starts_with_no_session(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        assert bot._chat_id is None
        assert bot._current_session_id is None

    def test_loads_persisted_state(self, tmp_path: Path) -> None:
        """Bot loads chat_id and session_id from state.json."""
        state_dir = tmp_path / "telegram"
        state_dir.mkdir(parents=True)
        state_path = state_dir / "state.json"
        state_path.write_text(
            json.dumps(
                {
                    "chat_id": 999,
                    "session_id": "abc-123",
                }
            )
        )

        bot = _make_bot(tmp_path)
        assert bot._chat_id == 999
        assert bot._current_session_id == "abc-123"


# ── Authorization ────────────────────────────────────────────────


class TestBotAuthorization:
    def test_authorized_chat_id(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path, allowed_chat_ids=[111, 222])
        assert bot._is_authorized(111) is True
        assert bot._is_authorized(222) is True

    def test_unauthorized_chat_id(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path, allowed_chat_ids=[111])
        assert bot._is_authorized(999) is False

    def test_empty_allowlist_denies_all(self, tmp_path: Path) -> None:
        """Empty allowed_chat_ids = fail-closed (deny all)."""
        bot = _make_bot(tmp_path, allowed_chat_ids=[])
        assert bot._is_authorized(123) is False


# ── Command Handlers ─────────────────────────────────────────────


class TestHandleStart:
    @pytest.mark.asyncio
    async def test_stores_chat_id_and_creates_session(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        update = _make_update(chat_id=123)
        await bot._handle_start(update, MagicMock())

        assert bot._chat_id == 123
        assert bot._current_session_id is not None
        update.message.reply_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_rejects_unauthorized(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path, allowed_chat_ids=[111])
        update = _make_update(chat_id=999)
        await bot._handle_start(update, MagicMock())

        assert bot._chat_id is None
        update.message.reply_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_emits_telemetry(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        update = _make_update(chat_id=123)
        await bot._handle_start(update, MagicMock())

        bot._telemetry.record_event.assert_called()
        event_name = bot._telemetry.record_event.call_args[0][0]
        assert event_name == "telegram:message_received"

    @pytest.mark.asyncio
    async def test_persists_state(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        update = _make_update(chat_id=123)
        await bot._handle_start(update, MagicMock())

        state_path = tmp_path / "telegram" / "state.json"
        assert state_path.exists()
        data = json.loads(state_path.read_text())
        assert data["chat_id"] == 123
        assert data["session_id"] == bot._current_session_id


class TestHandleNew:
    @pytest.mark.asyncio
    async def test_creates_new_session(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        # Set up initial session via /start
        start_update = _make_update(chat_id=123)
        await bot._handle_start(start_update, MagicMock())
        old_session = bot._current_session_id

        # /new creates a new session
        new_update = _make_update(chat_id=123)
        await bot._handle_new(new_update, MagicMock())

        assert bot._current_session_id != old_session
        assert bot._current_session_id is not None

    @pytest.mark.asyncio
    async def test_rejects_unauthorized(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path, allowed_chat_ids=[111])
        update = _make_update(chat_id=999)
        await bot._handle_new(update, MagicMock())

        update.message.reply_text.assert_not_called()


class TestHandleStatus:
    @pytest.mark.asyncio
    async def test_returns_session_info(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        # Set up session
        start_update = _make_update(chat_id=123)
        await bot._handle_start(start_update, MagicMock())

        status_update = _make_update(chat_id=123)
        await bot._handle_status(status_update, MagicMock())

        reply_text = status_update.message.reply_text.call_args[0][0]
        assert "Session:" in reply_text
        assert "Chat ID: 123" in reply_text
        assert "Queue:" in reply_text

    @pytest.mark.asyncio
    async def test_rejects_unauthorized(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path, allowed_chat_ids=[111])
        update = _make_update(chat_id=999)
        await bot._handle_status(update, MagicMock())

        update.message.reply_text.assert_not_called()


# ── Message Handler ──────────────────────────────────────────────


class TestHandleMessage:
    @pytest.mark.asyncio
    async def test_enqueues_authorized_message(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        update = _make_update(chat_id=123, text="test message")
        await bot._handle_message(update, MagicMock())

        assert bot._message_queue.qsize() == 1
        item = bot._message_queue.get_nowait()
        assert item["text"] == "test message"
        assert item["chat_id"] == 123

    @pytest.mark.asyncio
    async def test_rejects_unauthorized_message(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path, allowed_chat_ids=[111])
        update = _make_update(chat_id=999, text="sneaky")
        await bot._handle_message(update, MagicMock())

        assert bot._message_queue.qsize() == 0

    @pytest.mark.asyncio
    async def test_sends_typing_indicator(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        update = _make_update(chat_id=123, text="hello")
        await bot._handle_message(update, MagicMock())

        update.effective_chat.send_action.assert_called_once_with("typing")

    @pytest.mark.asyncio
    async def test_creates_session_if_none(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        assert bot._current_session_id is None

        update = _make_update(chat_id=123, text="hello")
        await bot._handle_message(update, MagicMock())

        assert bot._current_session_id is not None

    @pytest.mark.asyncio
    async def test_skips_empty_text(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        update = _make_update(chat_id=123)
        update.message.text = None
        await bot._handle_message(update, MagicMock())

        assert bot._message_queue.qsize() == 0


# ── Process Message ──────────────────────────────────────────────


class TestProcessMessage:
    @pytest.mark.asyncio
    async def test_calls_agent_chat_fn(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        bot._current_session_id = "test-session"

        mock_chat_fn = AsyncMock(return_value=MagicMock(content="Hello back"))
        bot.set_agent_chat_fn(mock_chat_fn)

        update = _make_update(chat_id=123, text="hi agent")
        item = {"text": "hi agent", "chat_id": 123, "update": update}
        await bot._process_message(item)

        mock_chat_fn.assert_called_once_with("hi agent", session_id="test-session")
        update.message.reply_text.assert_called_once_with("Hello back")

    @pytest.mark.asyncio
    async def test_splits_long_response(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path, max_message_length=20)
        bot._current_session_id = "test-session"

        long_text = "a" * 50
        mock_chat_fn = AsyncMock(return_value=MagicMock(content=long_text))
        bot.set_agent_chat_fn(mock_chat_fn)

        update = _make_update(chat_id=123)
        item = {"text": "hi", "chat_id": 123, "update": update}
        await bot._process_message(item)

        assert update.message.reply_text.call_count == 3

    @pytest.mark.asyncio
    async def test_handles_no_response(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        bot._current_session_id = "test-session"

        mock_chat_fn = AsyncMock(return_value=None)
        bot.set_agent_chat_fn(mock_chat_fn)

        update = _make_update(chat_id=123)
        item = {"text": "hi", "chat_id": 123, "update": update}
        await bot._process_message(item)

        update.message.reply_text.assert_called_once_with("(No response)")

    @pytest.mark.asyncio
    async def test_skips_if_no_chat_fn(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        update = _make_update(chat_id=123)
        item = {"text": "hi", "chat_id": 123, "update": update}
        await bot._process_message(item)

        update.message.reply_text.assert_called_once_with(
            "Agent not ready — chat function not bound."
        )

    @pytest.mark.asyncio
    async def test_emits_message_sent_event(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        bot._current_session_id = "test-session"

        mock_chat_fn = AsyncMock(return_value=MagicMock(content="reply"))
        bot.set_agent_chat_fn(mock_chat_fn)

        update = _make_update(chat_id=123)
        item = {"text": "hi", "chat_id": 123, "update": update}
        await bot._process_message(item)

        # Find the telegram:message_sent event
        calls = bot._telemetry.record_event.call_args_list
        event_names = [c[0][0] for c in calls]
        assert "telegram:message_sent" in event_names


# ── Send Notification ────────────────────────────────────────────


class TestSendNotification:
    @pytest.mark.asyncio
    async def test_sends_to_stored_chat_id(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        bot._chat_id = 123
        bot._application = MagicMock()
        bot._application.bot = MagicMock()
        bot._application.bot.send_message = AsyncMock()

        await bot.send_notification("Task completed!")

        bot._application.bot.send_message.assert_called_once_with(
            chat_id=123, text="Task completed!"
        )

    @pytest.mark.asyncio
    async def test_skips_if_no_chat_id(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        bot._chat_id = None
        # Should not raise
        await bot.send_notification("hello")

    @pytest.mark.asyncio
    async def test_skips_if_bot_not_running(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        bot._chat_id = 123
        bot._application = None
        # Should not raise
        await bot.send_notification("hello")

    @pytest.mark.asyncio
    async def test_emits_notification_event(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        bot._chat_id = 123
        bot._application = MagicMock()
        bot._application.bot = MagicMock()
        bot._application.bot.send_message = AsyncMock()

        await bot.send_notification("Done!")

        calls = bot._telemetry.record_event.call_args_list
        event_names = [c[0][0] for c in calls]
        assert "telegram:notification_sent" in event_names


# ── Start / Token Handling ───────────────────────────────────────


class TestBotStart:
    @pytest.mark.asyncio
    async def test_stays_dormant_without_token(self, tmp_path: Path) -> None:
        """If bot token env var is not set, bot stays dormant."""
        bot = _make_bot(tmp_path)
        with patch.dict("os.environ", {}, clear=True):
            await bot.start()

        assert bot._application is None
        assert bot._running is False


# ── State Persistence ────────────────────────────────────────────


class TestStatePersistence:
    def test_save_and_load(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        bot._chat_id = 456
        bot._current_session_id = "sess-xyz"
        bot._save_state()

        bot2 = _make_bot(tmp_path)
        assert bot2._chat_id == 456
        assert bot2._current_session_id == "sess-xyz"

    def test_handles_corrupt_state(self, tmp_path: Path) -> None:
        """Corrupt state file should not crash — starts fresh."""
        state_dir = tmp_path / "telegram"
        state_dir.mkdir(parents=True)
        (state_dir / "state.json").write_text("not valid json{{{")

        bot = _make_bot(tmp_path)
        assert bot._chat_id is None
        assert bot._current_session_id is None
