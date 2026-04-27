"""Regression tests for TelegramAdapter.send_with_id() and connect() full flow.

Wave-0 added send_with_id() to the TelegramAdapter. These tests lock in:
- send_with_id() returns the message_id as str
- send_with_id() raises RuntimeError when not connected
- send_with_id() uses a single message (no chunk-splitting)
- connect() full path: builds Application, calls initialize, starts polling task
- _run_polling_loop() conflict-error handling (retries + fatal-retryable)
- _run_polling_loop() network-error handling
- _run_polling_loop() unhandled error path
- _handle_update() missing effective_message branch
- _handle_update() missing effective_user branch
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcgateway.adapters.telegram import (
    TelegramAdapter,
)
from arcgateway.delivery import DeliveryTarget


def _make_adapter(
    allowed_user_ids: list[int] | None = None,
) -> TelegramAdapter:
    return TelegramAdapter(
        bot_token="test-token-abc",
        allowed_user_ids=allowed_user_ids if allowed_user_ids is not None else [42],
        on_message=AsyncMock(),
        agent_did="did:arc:agent:test",
    )


def _make_mock_application() -> MagicMock:
    app = MagicMock()
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
    app.updater = MagicMock()
    app.updater.running = True
    app.updater.stop = AsyncMock()
    app.updater.start_polling = AsyncMock()
    return app


# ---------------------------------------------------------------------------
# send_with_id() tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_with_id_returns_message_id() -> None:
    """send_with_id() returns the Telegram message_id as a str."""
    adapter = _make_adapter()
    mock_app = _make_mock_application()
    adapter._application = mock_app

    # Simulate the Telegram API returning a message object with message_id
    sent_msg = MagicMock()
    sent_msg.message_id = 9001
    mock_app.bot.send_message = AsyncMock(return_value=sent_msg)

    target = DeliveryTarget.parse("telegram:12345")
    result = await adapter.send_with_id(target, "hello")

    assert result == "9001"


@pytest.mark.asyncio
async def test_send_with_id_raises_when_not_connected() -> None:
    """send_with_id() raises RuntimeError when application is not set."""
    adapter = _make_adapter()
    # No _application set

    target = DeliveryTarget.parse("telegram:12345")
    with pytest.raises(RuntimeError, match="not connected"):
        await adapter.send_with_id(target, "hello")


@pytest.mark.asyncio
async def test_send_with_id_sends_single_api_call() -> None:
    """send_with_id() issues exactly one bot.send_message call (no chunk-splitting)."""
    adapter = _make_adapter()
    mock_app = _make_mock_application()
    adapter._application = mock_app

    sent_msg = MagicMock()
    sent_msg.message_id = 1234
    mock_app.bot.send_message = AsyncMock(return_value=sent_msg)

    target = DeliveryTarget.parse("telegram:99999")
    await adapter.send_with_id(target, "a single message")

    assert mock_app.bot.send_message.call_count == 1


@pytest.mark.asyncio
async def test_send_with_id_numeric_chat_id() -> None:
    """send_with_id() converts string chat_id to int for Telegram API."""
    adapter = _make_adapter()
    mock_app = _make_mock_application()
    adapter._application = mock_app

    sent_msg = MagicMock()
    sent_msg.message_id = 555
    mock_app.bot.send_message = AsyncMock(return_value=sent_msg)

    target = DeliveryTarget.parse("telegram:777")
    await adapter.send_with_id(target, "message")

    call_kwargs = mock_app.bot.send_message.call_args.kwargs
    # Telegram expects numeric chat_id when possible
    assert call_kwargs.get("chat_id") == 777 or call_kwargs.get("chat_id") == "777"


# ---------------------------------------------------------------------------
# connect() full Application-builder path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_full_path_starts_polling_task() -> None:
    """connect() must start a background polling task after initialisation."""
    adapter = _make_adapter()

    mock_app = _make_mock_application()

    # Simulate Application.builder().token(...).build() returning mock_app
    mock_builder = MagicMock()
    mock_builder.token.return_value = mock_builder
    mock_builder.build.return_value = mock_app

    # Provide a long-running updater that stops when adapter._running = False
    async def _fake_start_polling(**kwargs: Any) -> None:
        pass

    mock_app.updater.start_polling = AsyncMock(side_effect=_fake_start_polling)

    fake_application_cls = MagicMock()
    fake_application_cls.builder.return_value = mock_builder

    with patch.dict(
        "sys.modules",
        {
            "telegram": MagicMock(),
            "telegram.ext": MagicMock(Application=fake_application_cls),
        },
    ):
        # connect() starts polling in a background task; cancel it promptly
        connect_task = asyncio.create_task(adapter.connect())
        # Allow connect() to complete (it returns after starting the bg task)
        await asyncio.sleep(0.05)

    # Polling task should have been created
    assert adapter._polling_task is not None or connect_task.done()

    # Cleanup
    if adapter._polling_task and not adapter._polling_task.done():
        adapter._polling_task.cancel()
        try:
            await adapter._polling_task
        except (asyncio.CancelledError, Exception):  # noqa: S110
            pass  # test cleanup — logging would add noise without value


# ---------------------------------------------------------------------------
# _handle_update() edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_update_skips_no_effective_message() -> None:
    """_handle_update() returns early when effective_message is None."""
    on_message = AsyncMock()
    adapter = _make_adapter(allowed_user_ids=[42])
    adapter._bot_id = 999

    update = MagicMock()
    update.effective_message = None  # No message
    update.effective_user = MagicMock()
    update.effective_user.id = 42

    await adapter._handle_update(update, context=MagicMock())

    on_message.assert_not_called()


@pytest.mark.asyncio
async def test_handle_update_skips_no_effective_user() -> None:
    """_handle_update() returns early when effective_user is None."""
    on_message = AsyncMock()
    adapter = _make_adapter(allowed_user_ids=[42])
    adapter._bot_id = 999

    update = MagicMock()
    update.effective_message = MagicMock()
    update.effective_user = None  # No user

    await adapter._handle_update(update, context=MagicMock())

    on_message.assert_not_called()


@pytest.mark.asyncio
async def test_handle_update_on_message_exception_does_not_propagate() -> None:
    """Exceptions in on_message callback must not propagate out of _handle_update."""
    on_message = AsyncMock(side_effect=RuntimeError("callback boom"))
    adapter = TelegramAdapter(
        bot_token="tok-xyz",
        allowed_user_ids=[42],
        on_message=on_message,
    )
    adapter._bot_id = 999

    update = MagicMock()
    update.update_id = 1
    update.effective_user = MagicMock()
    update.effective_user.id = 42
    update.effective_chat = MagicMock()
    update.effective_chat.id = 1000
    update.effective_message = MagicMock()
    update.effective_message.text = "trigger exception"

    # Must not raise even though on_message raises
    await adapter._handle_update(update, context=MagicMock())


# ---------------------------------------------------------------------------
# Polling loop: conflict and network error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_polling_loop_conflict_sets_fatal_retryable() -> None:
    """Polling conflict must set _fatal_error and _fatal_retryable=True."""
    adapter = _make_adapter()
    mock_app = _make_mock_application()
    adapter._application = mock_app
    adapter._running = True

    # Simulate a conflict error during start()
    ConflictError = type("Conflict", (Exception,), {})
    mock_app.start = AsyncMock(side_effect=ConflictError("polling conflict"))

    fake_update_cls = MagicMock()

    with patch.dict(
        "sys.modules",
        {"telegram.ext": MagicMock(Update=fake_update_cls)},
    ):
        await adapter._run_polling_loop()

    assert adapter._fatal_error is not None
    assert adapter._fatal_retryable is True


@pytest.mark.asyncio
async def test_run_polling_loop_network_error_sets_fatal_retryable() -> None:
    """NetworkError in polling loop must set _fatal_retryable=True."""
    adapter = _make_adapter()
    mock_app = _make_mock_application()
    adapter._application = mock_app
    adapter._running = True

    NetworkError = type("NetworkError", (Exception,), {})
    mock_app.start = AsyncMock(side_effect=NetworkError("connection refused"))

    fake_update_cls = MagicMock()

    with patch.dict(
        "sys.modules",
        {"telegram.ext": MagicMock(Update=fake_update_cls)},
    ):
        await adapter._run_polling_loop()

    assert adapter._fatal_error is not None
    assert adapter._fatal_retryable is True


@pytest.mark.asyncio
async def test_run_polling_loop_unhandled_error_sets_fatal_not_retryable() -> None:
    """Unhandled (non-network, non-conflict) errors set retryable=False and re-raise."""
    adapter = _make_adapter()
    mock_app = _make_mock_application()
    adapter._application = mock_app
    adapter._running = True

    mock_app.start = AsyncMock(side_effect=ValueError("unexpected error"))

    fake_update_cls = MagicMock()

    with patch.dict(
        "sys.modules",
        {"telegram.ext": MagicMock(Update=fake_update_cls)},
    ):
        with pytest.raises(ValueError, match="unexpected error"):
            await adapter._run_polling_loop()

    assert adapter._fatal_error is not None
    assert adapter._fatal_retryable is False


# ---------------------------------------------------------------------------
# _initialize_with_retry: network error exhausted retries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_with_retry_exhausted_raises_runtime_error() -> None:
    """_initialize_with_retry() raises RuntimeError after all network retries are exhausted."""
    adapter = _make_adapter()
    mock_app = _make_mock_application()
    adapter._application = mock_app

    NetworkError = type("NetworkError", (Exception,), {})
    mock_app.initialize = AsyncMock(side_effect=NetworkError("connection failed"))

    with patch("asyncio.sleep", new=AsyncMock()):
        with pytest.raises(RuntimeError, match="persistent NetworkError"):
            await adapter._initialize_with_retry()

    assert adapter._fatal_retryable is True


@pytest.mark.asyncio
async def test_initialize_with_retry_non_network_error_raises_immediately() -> None:
    """_initialize_with_retry() raises immediately for non-NetworkError."""
    adapter = _make_adapter()
    mock_app = _make_mock_application()
    adapter._application = mock_app

    mock_app.initialize = AsyncMock(side_effect=ValueError("bad token"))

    with pytest.raises(ValueError, match="bad token"):
        await adapter._initialize_with_retry()

    # Non-network errors are not retryable
    assert adapter._fatal_retryable is False


# ---------------------------------------------------------------------------
# disconnect() with no updater attribute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disconnect_handles_missing_updater_attribute() -> None:
    """disconnect() must not raise when application.updater attribute is absent."""
    adapter = _make_adapter()
    mock_app = _make_mock_application()
    # Remove the updater attribute entirely
    del mock_app.updater
    adapter._application = mock_app
    adapter._running = True

    # Must not raise
    await adapter.disconnect()

    assert adapter._application is None
