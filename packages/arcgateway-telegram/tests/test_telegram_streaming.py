"""Tests for TelegramAdapter streaming primitives: edit_message + send_typing.

These give Telegram the same progressive-streaming experience as Slack: the
placeholder message is edited in place as tokens arrive, with a typing
indicator shown before the first content.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from arcgateway.delivery import DeliveryTarget

from arcgateway_telegram.adapter import _TELEGRAM_MAX_MESSAGE_LENGTH, TelegramAdapter


def _make_adapter() -> TelegramAdapter:
    return TelegramAdapter(
        bot_token="test-token-abc",
        allowed_user_ids=[42],
        on_message=AsyncMock(),
        agent_did="did:arc:agent:test",
    )


def _connected_adapter() -> TelegramAdapter:
    adapter = _make_adapter()
    app = MagicMock()
    app.bot.edit_message_text = AsyncMock()
    app.bot.send_chat_action = AsyncMock()
    adapter._application = app
    return adapter


@pytest.mark.asyncio
async def test_edit_message_calls_edit_message_text() -> None:
    adapter = _connected_adapter()
    target = DeliveryTarget.parse("telegram:12345")

    await adapter.edit_message(target, "678", "partial reply")

    adapter._application.bot.edit_message_text.assert_awaited_once()
    kwargs = adapter._application.bot.edit_message_text.await_args.kwargs
    assert kwargs["chat_id"] == 12345
    assert kwargs["message_id"] == 678
    assert kwargs["text"] == "partial reply"


@pytest.mark.asyncio
async def test_edit_message_truncates_to_telegram_limit() -> None:
    adapter = _connected_adapter()
    target = DeliveryTarget.parse("telegram:12345")

    await adapter.edit_message(target, "1", "x" * (_TELEGRAM_MAX_MESSAGE_LENGTH + 500))

    kwargs = adapter._application.bot.edit_message_text.await_args.kwargs
    assert len(kwargs["text"]) == _TELEGRAM_MAX_MESSAGE_LENGTH


@pytest.mark.asyncio
async def test_edit_message_raises_when_not_connected() -> None:
    adapter = _make_adapter()
    with pytest.raises(RuntimeError, match="not connected"):
        await adapter.edit_message(DeliveryTarget.parse("telegram:1"), "1", "hi")


@pytest.mark.asyncio
async def test_send_typing_calls_send_chat_action() -> None:
    adapter = _connected_adapter()
    target = DeliveryTarget.parse("telegram:12345")

    await adapter.send_typing(target)

    adapter._application.bot.send_chat_action.assert_awaited_once()
    kwargs = adapter._application.bot.send_chat_action.await_args.kwargs
    assert kwargs["chat_id"] == 12345
    assert str(kwargs["action"]) == "typing"


@pytest.mark.asyncio
async def test_send_typing_noop_when_not_connected() -> None:
    adapter = _make_adapter()
    # Must not raise.
    await adapter.send_typing(DeliveryTarget.parse("telegram:1"))


def test_adapter_exposes_streaming_methods() -> None:
    """StreamBridge probes these via hasattr — they must exist on the class."""
    assert hasattr(TelegramAdapter, "edit_message")
    assert hasattr(TelegramAdapter, "send_typing")
