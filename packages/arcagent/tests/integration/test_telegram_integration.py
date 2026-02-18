"""Integration tests — Telegram Module end-to-end with Module Bus.

Tests real component interactions: Module Bus → TelegramModule → TelegramBot
with mocked python-telegram-bot at the library boundary.
External dependencies (python-telegram-bot, ArcLLM, ArcRun) are stubbed.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.core.config import AgentConfig, ArcAgentConfig, LLMConfig
from arcagent.core.module_bus import ModuleBus, ModuleContext
from arcagent.core.telemetry import AgentTelemetry
from arcagent.modules.telegram import TelegramModule
from arcagent.modules.telegram.bot import TelegramBot


def _make_telemetry() -> AgentTelemetry:
    t = MagicMock(spec=AgentTelemetry)
    t.audit_event = MagicMock()
    t.record_event = MagicMock()
    return t


def _make_bus() -> ModuleBus:
    config = ArcAgentConfig(
        agent=AgentConfig(name="test", workspace="./test-workspace"),
        llm=LLMConfig(model="test/model"),
    )
    telemetry = _make_telemetry()
    return ModuleBus(config=config, telemetry=telemetry)


def _make_module_ctx(bus: ModuleBus, workspace: Path) -> ModuleContext:
    config = ArcAgentConfig(
        agent=AgentConfig(name="test", workspace=str(workspace)),
        llm=LLMConfig(model="test/model"),
    )
    return ModuleContext(
        bus=bus,
        tool_registry=MagicMock(),
        config=config,
        telemetry=_make_telemetry(),
        workspace=workspace,
        llm_config=config.llm,
    )


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


class TestFullMessageFlow:
    """5.1 — Full message flow: inbound → agent.chat() → response sent."""

    @pytest.mark.asyncio
    async def test_message_routed_to_agent_chat(self, tmp_path: Path) -> None:
        """Message received → agent.chat() called → response sent back."""
        module = TelegramModule(
            config={"enabled": True, "allowed_chat_ids": [123]},
            telemetry=_make_telemetry(),
            workspace=tmp_path,
        )
        bus = _make_bus()
        ctx = _make_module_ctx(bus, tmp_path)

        # Start module (bot stays dormant — no token env var)
        await module.startup(ctx)

        # Manually wire chat fn and process a message
        mock_chat = AsyncMock(return_value=MagicMock(content="Hello human!"))
        module.set_agent_chat_fn(mock_chat)

        bot = module._bot
        assert bot is not None

        # Set session state
        bot._current_session_id = "test-session"

        update = _make_update(chat_id=123, text="Hi agent")
        item = {"text": "Hi agent", "chat_id": 123, "update": update}
        await bot._process_message(item)

        mock_chat.assert_called_once_with("Hi agent", session_id="test-session")
        update.message.reply_text.assert_called_once_with("Hello human!")

    @pytest.mark.asyncio
    async def test_session_state_persists(self, tmp_path: Path) -> None:
        """State file is created and persists across module restarts."""
        module = TelegramModule(
            config={"enabled": True, "allowed_chat_ids": [123]},
            telemetry=_make_telemetry(),
            workspace=tmp_path,
        )
        bus = _make_bus()
        ctx = _make_module_ctx(bus, tmp_path)
        await module.startup(ctx)

        bot = module._bot
        assert bot is not None

        # Simulate /start
        update = _make_update(chat_id=123)
        await bot._handle_start(update, MagicMock())

        stored_session = bot._current_session_id
        assert stored_session is not None

        # Verify state.json was written
        state_path = tmp_path / "telegram" / "state.json"
        assert state_path.exists()
        data = json.loads(state_path.read_text())
        assert data["chat_id"] == 123
        assert data["session_id"] == stored_session

        await module.shutdown()

        # Create new module — should load persisted state
        module2 = TelegramModule(
            config={"enabled": True, "allowed_chat_ids": [123]},
            telemetry=_make_telemetry(),
            workspace=tmp_path,
        )
        bus2 = _make_bus()
        ctx2 = _make_module_ctx(bus2, tmp_path)
        await module2.startup(ctx2)

        bot2 = module2._bot
        assert bot2 is not None
        assert bot2._chat_id == 123
        assert bot2._current_session_id == stored_session

        await module2.shutdown()


class TestProactiveNotification:
    """5.2 — Agent-driven notifications via notify_user tool."""

    @pytest.mark.asyncio
    async def test_notify_user_tool_sends_to_telegram(self, tmp_path: Path) -> None:
        module = TelegramModule(
            config={"enabled": True, "allowed_chat_ids": [123]},
            telemetry=_make_telemetry(),
            workspace=tmp_path,
        )
        bus = _make_bus()
        ctx = _make_module_ctx(bus, tmp_path)
        await module.startup(ctx)

        bot = module._bot
        assert bot is not None

        # Set up bot with a stored chat_id and mock application
        bot._chat_id = 123
        mock_app = MagicMock()
        mock_app.bot = MagicMock()
        mock_app.bot.send_message = AsyncMock()
        mock_app.updater = MagicMock()
        mock_app.updater.running = False
        mock_app.stop = AsyncMock()
        mock_app.shutdown = AsyncMock()
        bot._application = mock_app

        # Get the registered notify_user tool and invoke it
        tool = ctx.tool_registry.register.call_args[0][0]
        assert tool.name == "notify_user"

        import json
        result = await tool.execute(message="Found critical issue in module X")
        data = json.loads(result)
        assert data["status"] == "sent"

        mock_app.bot.send_message.assert_called_once()
        sent_text = mock_app.bot.send_message.call_args[1]["text"]
        assert "critical issue" in sent_text

        await module.shutdown()


class TestAuthorizationIntegration:
    """5.3 — Authorization enforcement with real Module Bus."""

    @pytest.mark.asyncio
    async def test_unauthorized_message_silently_rejected(self, tmp_path: Path) -> None:
        module = TelegramModule(
            config={"enabled": True, "allowed_chat_ids": [111]},
            telemetry=_make_telemetry(),
            workspace=tmp_path,
        )
        bus = _make_bus()
        ctx = _make_module_ctx(bus, tmp_path)
        await module.startup(ctx)

        bot = module._bot
        assert bot is not None

        # Set up a mock chat fn that should NOT be called
        mock_chat = AsyncMock()
        module.set_agent_chat_fn(mock_chat)

        # Unauthorized chat_id (999 not in allowed_chat_ids [111])
        update = _make_update(chat_id=999, text="sneaky message")
        await bot._handle_message(update, MagicMock())

        # Should not enqueue — queue stays empty
        assert bot._message_queue.qsize() == 0

        # Verify telemetry recorded auth rejection
        telemetry_calls = bot._telemetry.record_event.call_args_list
        event_names = [c[0][0] for c in telemetry_calls]
        assert "telegram:auth_rejected" in event_names

        await module.shutdown()

    @pytest.mark.asyncio
    async def test_authorized_message_proceeds(self, tmp_path: Path) -> None:
        module = TelegramModule(
            config={"enabled": True, "allowed_chat_ids": [111]},
            telemetry=_make_telemetry(),
            workspace=tmp_path,
        )
        bus = _make_bus()
        ctx = _make_module_ctx(bus, tmp_path)
        await module.startup(ctx)

        bot = module._bot
        assert bot is not None

        # Authorized chat_id
        update = _make_update(chat_id=111, text="valid message")
        await bot._handle_message(update, MagicMock())

        assert bot._message_queue.qsize() == 1

        await module.shutdown()
