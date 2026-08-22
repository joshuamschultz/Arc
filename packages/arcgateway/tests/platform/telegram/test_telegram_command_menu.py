"""setMyCommands wiring for the Telegram native "/" menu.

Telegram delivers slash commands as ordinary message text, so the adapter needs
nothing to *dispatch* them. What it does publish is the native command menu via
``setMyCommands`` on connect. These tests prove ``set_command_names`` records the
specs and ``_publish_command_menu`` calls the bot with a ``BotCommand`` list,
skipping any name Telegram would reject.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import BotCommand

from arcgateway.adapters.telegram.adapter import TelegramAdapter
from arcgateway.commands.base import CommandSpec


def _make_adapter() -> TelegramAdapter:
    return TelegramAdapter(
        bot_token="123:abc",
        allowed_user_ids=[42],
        on_message=AsyncMock(),
        agent_did="did:arc:agent:test",
    )


def test_set_command_names_records_specs() -> None:
    adapter = _make_adapter()
    specs = [
        CommandSpec("new", "Start a new session"),
        CommandSpec("briefing", "Morning briefing"),
    ]
    adapter.set_command_names(specs)
    assert adapter._command_specs == tuple(specs)


@pytest.mark.asyncio
async def test_publish_command_menu_calls_set_my_commands() -> None:
    adapter = _make_adapter()
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()
    application = MagicMock()
    application.bot = bot
    adapter._application = application

    adapter.set_command_names(
        [CommandSpec("new", "Start a new session"), CommandSpec("briefing", "Morning briefing")]
    )
    await adapter._publish_command_menu()

    bot.set_my_commands.assert_awaited_once()
    (commands,) = bot.set_my_commands.await_args.args
    assert all(isinstance(c, BotCommand) for c in commands)
    assert [c.command for c in commands] == ["new", "briefing"]
    assert [c.description for c in commands] == ["Start a new session", "Morning briefing"]


@pytest.mark.asyncio
async def test_publish_command_menu_skips_illegal_names() -> None:
    adapter = _make_adapter()
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()
    application = MagicMock()
    application.bot = bot
    adapter._application = application

    # "New Session" has a space and capitals — Telegram rejects it; skip, keep the rest.
    adapter.set_command_names(
        [CommandSpec("New Session", "bad"), CommandSpec("help", "Show help")]
    )
    await adapter._publish_command_menu()

    (commands,) = bot.set_my_commands.await_args.args
    assert [c.command for c in commands] == ["help"]


@pytest.mark.asyncio
async def test_publish_command_menu_truncates_description() -> None:
    adapter = _make_adapter()
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()
    application = MagicMock()
    application.bot = bot
    adapter._application = application

    adapter.set_command_names([CommandSpec("run", "x" * 400)])
    await adapter._publish_command_menu()

    (commands,) = bot.set_my_commands.await_args.args
    assert len(commands[0].description) == 256


@pytest.mark.asyncio
async def test_publish_command_menu_noop_without_specs() -> None:
    adapter = _make_adapter()
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()
    application = MagicMock()
    application.bot = bot
    adapter._application = application

    await adapter._publish_command_menu()  # no specs recorded
    bot.set_my_commands.assert_not_awaited()
