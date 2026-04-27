"""Unit tests for arcgateway.adapters.telegram.TelegramAdapter.

All python-telegram-bot calls are mocked so tests run without the library
installed (simulates an environment where only the base arcgateway dep is
present, and the telegram optional extra is absent).

Test coverage:
    - connect() initialises the bot (T1.7.1)
    - disconnect() stops polling cleanly
    - send() uses bot API with correct chat_id and splits long messages
    - Unauthorized user emits audit + does NOT call on_message
    - Message from authorized user wraps InboundEvent + calls on_message
    - split_message() boundary logic
    - _is_conflict_error() / _is_network_error() helpers
    - _network_backoff() stays within cap
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# We import the module under test regardless of whether python-telegram-bot is
# installed — the adapter's connect() guards that import internally.
from arcgateway.adapters.telegram import (
    TelegramAdapter,
    _is_conflict_error,
    _is_network_error,
    _network_backoff,
    split_message,
)
from arcgateway.delivery import DeliveryTarget
from arcgateway.executor import InboundEvent

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_adapter(
    allowed_user_ids: list[int] | None = None,
    on_message: Any = None,
) -> TelegramAdapter:
    """Create a TelegramAdapter with sensible test defaults."""
    if on_message is None:
        on_message = AsyncMock()
    return TelegramAdapter(
        bot_token="test-token-abc123",
        allowed_user_ids=allowed_user_ids if allowed_user_ids is not None else [42],
        on_message=on_message,
        agent_did="did:arc:agent:test",
    )


def _make_mock_application() -> MagicMock:
    """Build a fully-mocked python-telegram-bot Application."""
    app = MagicMock()
    # bot.get_me() returns an object with .id and .username
    bot_info = MagicMock()
    bot_info.id = 99
    bot_info.username = "test_bot"
    app.bot.get_me = AsyncMock(return_value=bot_info)
    app.bot.send_message = AsyncMock()
    app.initialize = AsyncMock()
    app.start = AsyncMock()
    app.stop = AsyncMock()
    app.shutdown = AsyncMock()
    app.add_handler = MagicMock()
    app.add_error_handler = MagicMock()
    # updater.running = True so disconnect attempts to stop it
    app.updater = MagicMock()
    app.updater.running = True
    app.updater.stop = AsyncMock()
    app.updater.start_polling = AsyncMock()
    return app


def _make_update(user_id: int = 42, text: str = "hello", update_id: int = 1) -> MagicMock:
    """Build a minimal mock Telegram Update."""
    update = MagicMock()
    update.update_id = update_id
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat = MagicMock()
    update.effective_chat.id = 1000
    update.effective_message = MagicMock()
    update.effective_message.text = text
    return update


# ── split_message() tests ─────────────────────────────────────────────────────


def test_split_message_short_text_returns_single_chunk() -> None:
    chunks = split_message("hello world")
    assert chunks == ["hello world"]


def test_split_message_empty_text_returns_empty_list() -> None:
    assert split_message("") == []


def test_split_message_splits_at_paragraph_boundary() -> None:
    text = ("A" * 3000) + "\n\n" + ("B" * 2000)
    chunks = split_message(text, max_length=4096)
    assert len(chunks) == 2
    assert chunks[0] == "A" * 3000
    assert chunks[1] == "B" * 2000


def test_split_message_hard_split_when_no_boundary() -> None:
    text = "X" * 5000
    chunks = split_message(text, max_length=4096)
    # First chunk is exactly max_length
    assert len(chunks[0]) == 4096
    # All content preserved
    assert "".join(chunks) == text


def test_split_message_exact_length_no_split() -> None:
    text = "A" * 4096
    chunks = split_message(text, max_length=4096)
    assert chunks == [text]


# ── _is_conflict_error() tests ────────────────────────────────────────────────


def test_is_conflict_error_with_conflict_class_name() -> None:
    exc = type("Conflict", (Exception,), {})("conflict")
    assert _is_conflict_error(exc)


def test_is_conflict_error_with_message_text() -> None:
    exc = Exception("terminated by other getUpdates request")
    assert _is_conflict_error(exc)


def test_is_conflict_error_returns_false_for_unrelated_error() -> None:
    exc = ValueError("something else")
    assert not _is_conflict_error(exc)


# ── _is_network_error() tests ─────────────────────────────────────────────────


def test_is_network_error_with_network_error_class() -> None:
    exc = type("NetworkError", (Exception,), {})("network failure")
    assert _is_network_error(exc)


def test_is_network_error_with_timed_out_class() -> None:
    exc = type("TimedOut", (Exception,), {})("timed out")
    assert _is_network_error(exc)


def test_is_network_error_returns_false_for_unrelated_error() -> None:
    exc = ValueError("value error")
    assert not _is_network_error(exc)


# ── _network_backoff() tests ──────────────────────────────────────────────────


def test_network_backoff_grows_exponentially() -> None:
    b1 = _network_backoff(1)
    b2 = _network_backoff(2)
    b3 = _network_backoff(3)
    assert b1 < b2 < b3


def test_network_backoff_caps_at_60s() -> None:
    assert _network_backoff(10) == 60.0
    assert _network_backoff(100) == 60.0


# ── TelegramAdapter constructor tests ────────────────────────────────────────


def test_adapter_name_is_telegram() -> None:
    adapter = _make_adapter()
    assert adapter.name == "telegram"


def test_adapter_rejects_empty_token() -> None:
    with pytest.raises(ValueError, match="bot_token"):
        TelegramAdapter(
            bot_token="",
            allowed_user_ids=[1],
            on_message=AsyncMock(),
        )


# ── connect() tests ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_calls_bot_initialize() -> None:
    """connect() must initialize the bot application (token verification).

    We test _initialize_with_retry() directly — it is the codepath connect()
    calls to verify the token and populate bot_id. The Application object is
    injected manually (avoids patching a module-level name that doesn't exist
    since Application is lazily imported inside connect()).
    """
    adapter = _make_adapter()
    mock_app = _make_mock_application()

    # Inject the mock application directly — this is the path connect() takes
    adapter._application = mock_app

    await adapter._initialize_with_retry()

    # Both must be called: initialize() verifies the token; get_me() fetches bot info.
    mock_app.initialize.assert_called_once()
    mock_app.bot.get_me.assert_called_once()


@pytest.mark.asyncio
async def test_connect_sets_bot_id() -> None:
    """connect() stores the bot's numeric ID from get_me()."""
    adapter = _make_adapter()
    mock_app = _make_mock_application()
    adapter._application = mock_app

    await adapter._initialize_with_retry()

    assert adapter._bot_id == 99


# ── disconnect() tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disconnect_stops_polling() -> None:
    """disconnect() stops the updater and shuts down the application."""
    adapter = _make_adapter()
    mock_app = _make_mock_application()
    adapter._application = mock_app
    adapter._running = True

    # Create a real asyncio task that will be cancelled by disconnect()
    async def _never_ending() -> None:
        await asyncio.sleep(9999)

    adapter._polling_task = asyncio.create_task(_never_ending())

    await adapter.disconnect()

    assert adapter._running is False
    assert adapter._application is None
    mock_app.updater.stop.assert_called_once()
    mock_app.stop.assert_called_once()
    mock_app.shutdown.assert_called_once()


@pytest.mark.asyncio
async def test_disconnect_is_idempotent() -> None:
    """disconnect() must not raise if called when already disconnected."""
    adapter = _make_adapter()
    # No application, no polling task
    await adapter.disconnect()  # Must not raise


# ── send() tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_calls_bot_send_message() -> None:
    """send() calls bot.send_message with correct chat_id."""
    adapter = _make_adapter()
    mock_app = _make_mock_application()
    adapter._application = mock_app

    target = DeliveryTarget.parse("telegram:12345")
    await adapter.send(target, "hello world")

    mock_app.bot.send_message.assert_called_once_with(
        chat_id=12345,
        text="hello world",
        reply_to_message_id=None,
    )


@pytest.mark.asyncio
async def test_send_splits_long_message() -> None:
    """send() splits messages exceeding 4096 chars into multiple API calls."""
    adapter = _make_adapter()
    mock_app = _make_mock_application()
    adapter._application = mock_app

    long_message = "X" * 5000
    target = DeliveryTarget.parse("telegram:12345")
    await adapter.send(target, long_message)

    # Should be called twice (5000 / 4096 ≈ 2 chunks)
    assert mock_app.bot.send_message.call_count == 2


@pytest.mark.asyncio
async def test_send_raises_when_not_connected() -> None:
    """send() raises RuntimeError if application is not connected."""
    adapter = _make_adapter()
    # Do NOT set adapter._application

    target = DeliveryTarget.parse("telegram:12345")
    with pytest.raises(RuntimeError, match="not connected"):
        await adapter.send(target, "hello")


@pytest.mark.asyncio
async def test_send_uses_reply_to_message_id() -> None:
    """send() passes reply_to as reply_to_message_id."""
    adapter = _make_adapter()
    mock_app = _make_mock_application()
    adapter._application = mock_app

    target = DeliveryTarget.parse("telegram:12345")
    await adapter.send(target, "hello", reply_to="777")

    mock_app.bot.send_message.assert_called_once_with(
        chat_id=12345,
        text="hello",
        reply_to_message_id=777,
    )


# ── Auth tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unauthorized_user_rejected() -> None:
    """Messages from non-allowlisted users must not call on_message.

    The adapter must emit a gateway.adapter.auth_rejected audit log and
    silently ignore the message (no reply = no info leakage).
    """
    on_message = AsyncMock()
    adapter = _make_adapter(allowed_user_ids=[100], on_message=on_message)
    adapter._bot_id = 999

    # user_id=42 is NOT in allowed_user_ids=[100]
    update = _make_update(user_id=42, text="inject this")

    await adapter._handle_update(update, context=MagicMock())

    # on_message must NOT be called
    on_message.assert_not_called()


@pytest.mark.asyncio
async def test_unauthorized_empty_allowlist_denies_all() -> None:
    """Empty allowed_user_ids means deny all — fail-closed behaviour."""
    on_message = AsyncMock()
    adapter = _make_adapter(allowed_user_ids=[], on_message=on_message)
    adapter._bot_id = 999

    update = _make_update(user_id=42, text="hello")
    await adapter._handle_update(update, context=MagicMock())

    on_message.assert_not_called()


@pytest.mark.asyncio
async def test_message_received_calls_on_message() -> None:
    """Real message wraps InboundEvent and calls on_message."""
    on_message = AsyncMock()
    adapter = _make_adapter(allowed_user_ids=[42], on_message=on_message)
    adapter._bot_id = 999

    update = _make_update(user_id=42, text="hello agent")
    await adapter._handle_update(update, context=MagicMock())

    on_message.assert_called_once()
    call_args = on_message.call_args[0]
    event: InboundEvent = call_args[0]
    assert isinstance(event, InboundEvent)
    assert event.platform == "telegram"
    assert event.message == "hello agent"
    assert "telegram:42" in event.user_did


@pytest.mark.asyncio
async def test_bot_own_messages_skipped() -> None:
    """The adapter must skip messages from itself to prevent self-talk loops."""
    on_message = AsyncMock()
    adapter = _make_adapter(allowed_user_ids=[99], on_message=on_message)
    adapter._bot_id = 99  # Same as the update's user_id

    update = _make_update(user_id=99, text="self message")
    await adapter._handle_update(update, context=MagicMock())

    on_message.assert_not_called()


@pytest.mark.asyncio
async def test_update_with_no_text_skipped() -> None:
    """Updates without text (photos, stickers, etc.) must be silently skipped."""
    on_message = AsyncMock()
    adapter = _make_adapter(allowed_user_ids=[42], on_message=on_message)
    adapter._bot_id = 999

    update = _make_update(user_id=42, text="")
    update.effective_message.text = None
    await adapter._handle_update(update, context=MagicMock())

    on_message.assert_not_called()


# ── Fatal error state tests ───────────────────────────────────────────────────


def test_set_fatal_error_retryable_true() -> None:
    adapter = _make_adapter()
    exc = Exception("boom")
    adapter._set_fatal_error(exc, retryable=True)

    assert adapter._fatal_error is exc
    assert adapter._fatal_retryable is True


def test_set_fatal_error_retryable_false() -> None:
    adapter = _make_adapter()
    exc = Exception("permanent")
    adapter._set_fatal_error(exc, retryable=False)

    assert adapter._fatal_error is exc
    assert adapter._fatal_retryable is False
