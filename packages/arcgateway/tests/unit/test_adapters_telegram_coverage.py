"""Additional coverage tests for TelegramAdapter edge cases.

Targets uncovered lines in telegram.py:
- split_message(): single-newline boundary (lines 121-123)
- split_message(): sentence-boundary fallback (lines 128, 130-133)
- send(): non-numeric chat_id falls back to string (lines 310-311)
- send(): invalid non-numeric reply_to logs warning (lines 317-318)
- send_with_id(): non-numeric chat_id (lines 373-374)
- disconnect(): exception during polling task cancellation (lines 265-266)
- disconnect(): exception during application shutdown (lines 276-277)
- _run_polling_loop(): ImportError from telegram.ext (lines 470-472)
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcgateway.adapters.telegram import TelegramAdapter, split_message
from arcgateway.delivery import DeliveryTarget


def _make_adapter(allowed_user_ids: list[int] | None = None) -> TelegramAdapter:
    return TelegramAdapter(
        bot_token="tok-cover",
        allowed_user_ids=allowed_user_ids if allowed_user_ids is not None else [1],
        on_message=AsyncMock(),
        agent_did="did:arc:agent:cover",
    )


def _make_mock_app() -> MagicMock:
    app = MagicMock()
    bot_info = MagicMock()
    bot_info.id = 1
    bot_info.username = "cover_bot"
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
# split_message(): boundary paths
# ---------------------------------------------------------------------------


def test_split_message_single_newline_boundary() -> None:
    """split_message uses single-newline boundary when no double-newline fits."""
    # 3500 chars + "\n" + 2000 chars = 5501 total; exceeds 4096
    line1 = "A" * 3500
    line2 = "B" * 2000
    text = line1 + "\n" + line2
    chunks = split_message(text, max_length=4096)
    assert len(chunks) == 2
    assert chunks[0] == line1
    assert chunks[1] == line2


def test_split_message_sentence_boundary_fallback() -> None:
    """split_message falls back to sentence boundary when no newline fits."""
    # Build text that is >4096 but has no newlines — only sentence boundaries.
    # "Word word word. " repeated to exceed 4096 chars in a single chunk.
    # Each repetition is 16 chars; 256 * 16 = 4096 exactly, so 260 * 16 = 4160.
    sentence_part = "Word word word. " * 260  # 4160 chars, no newlines
    assert len(sentence_part) > 4096
    chunks = split_message(sentence_part, max_length=4096)
    # Must split at a sentence boundary, not a hard cut
    assert len(chunks) >= 2
    # No chunk should start with a space (lstrip is applied)
    for chunk in chunks:
        assert not chunk.startswith(" ")


# ---------------------------------------------------------------------------
# send(): non-numeric chat_id and invalid reply_to
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_non_numeric_chat_id_uses_string() -> None:
    """send() uses chat_id as a string when it cannot be parsed as int."""
    adapter = _make_adapter()
    mock_app = _make_mock_app()
    adapter._application = mock_app

    target = DeliveryTarget(platform="telegram", chat_id="@my_channel")
    await adapter.send(target, "hello channel")

    call_kwargs = mock_app.bot.send_message.call_args.kwargs
    # chat_id stays as string because "@my_channel" is not numeric
    assert call_kwargs["chat_id"] == "@my_channel"


@pytest.mark.asyncio
async def test_send_invalid_reply_to_logs_warning_and_ignores() -> None:
    """send() logs a warning and ignores reply_to when it's not a valid int."""
    adapter = _make_adapter()
    mock_app = _make_mock_app()
    adapter._application = mock_app

    target = DeliveryTarget.parse("telegram:12345")
    # "abc" is not a valid message_id integer
    await adapter.send(target, "message with bad reply", reply_to="not-an-int")

    # send_message is still called (message is sent, just without reply_to)
    mock_app.bot.send_message.assert_called_once()
    call_kwargs = mock_app.bot.send_message.call_args.kwargs
    assert call_kwargs.get("reply_to_message_id") is None


# ---------------------------------------------------------------------------
# send_with_id(): non-numeric chat_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_with_id_non_numeric_chat_id() -> None:
    """send_with_id() handles non-numeric chat_id (string group names)."""
    adapter = _make_adapter()
    mock_app = _make_mock_app()
    adapter._application = mock_app

    sent = MagicMock()
    sent.message_id = 77
    mock_app.bot.send_message = AsyncMock(return_value=sent)

    target = DeliveryTarget(platform="telegram", chat_id="@groupchat")
    result = await adapter.send_with_id(target, "to group")

    call_kwargs = mock_app.bot.send_message.call_args.kwargs
    assert call_kwargs["chat_id"] == "@groupchat"
    assert result == "77"


# ---------------------------------------------------------------------------
# disconnect(): exception paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disconnect_handles_polling_task_exception() -> None:
    """disconnect() must not propagate exceptions from awaiting the polling task."""
    adapter = _make_adapter()
    mock_app = _make_mock_app()
    adapter._application = mock_app
    adapter._running = True

    async def _bad_task() -> None:
        raise RuntimeError("task error on await")

    adapter._polling_task = asyncio.create_task(_bad_task())
    # Allow the task to fail
    await asyncio.sleep(0)

    # disconnect() must not raise even though the task raised
    await adapter.disconnect()
    assert adapter._application is None


@pytest.mark.asyncio
async def test_disconnect_handles_application_shutdown_exception() -> None:
    """disconnect() must not propagate exceptions from application.stop/shutdown."""
    adapter = _make_adapter()
    mock_app = _make_mock_app()
    mock_app.stop = AsyncMock(side_effect=RuntimeError("stop failed"))
    adapter._application = mock_app
    adapter._running = True

    # No polling task
    # disconnect() must not raise even though stop() raises
    await adapter.disconnect()
    # application is set to None even on error
    assert adapter._application is None


# ---------------------------------------------------------------------------
# _run_polling_loop(): ImportError from missing telegram.ext
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_polling_loop_import_error_returns_cleanly() -> None:
    """_run_polling_loop() returns early (no exception) when telegram.ext is missing."""
    adapter = _make_adapter()
    adapter._running = True

    # Simulate telegram.ext not installed by patching the import inside the method
    with patch.dict("sys.modules", {"telegram.ext": None}):  # type: ignore[dict-item]
        # Must return cleanly (logs error and returns)
        await adapter._run_polling_loop()

    # No fatal error should be set — the method just returns early on ImportError
    # (This is the polite degradation path when optional dep is missing)
