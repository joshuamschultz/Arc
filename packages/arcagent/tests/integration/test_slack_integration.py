"""Integration tests — Slack Module end-to-end with Module Bus.

Tests real component interactions: Module Bus → SlackModule → SlackBot
with mocked slack-bolt at the library boundary.
External dependencies (slack-bolt, ArcLLM, ArcRun) are stubbed.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.core.config import AgentConfig, ArcAgentConfig, LLMConfig
from arcagent.core.module_bus import ModuleBus, ModuleContext
from arcagent.core.telemetry import AgentTelemetry
from arcagent.modules.slack import SlackModule
from arcagent.modules.slack.bot import SlackBot


def _make_telemetry() -> AgentTelemetry:
    t = MagicMock(spec=AgentTelemetry)
    t.audit_event = MagicMock()
    t.record_event = MagicMock()
    return t


def _make_bus() -> ModuleBus:
    return ModuleBus()


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


class TestFullMessageFlow:
    """Full message flow: inbound DM → agent.chat() → response sent."""

    @pytest.mark.asyncio
    async def test_message_routed_to_agent_chat(self, tmp_path: Path) -> None:
        """Message received → agent.chat() called → response sent back."""
        module = SlackModule(
            config={"enabled": True, "allowed_user_ids": ["U123"]},
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

        # Set session state and mock app
        bot._current_session_id = "test-session"
        bot._app = MagicMock()
        bot._app.client = MagicMock()
        bot._app.client.chat_postMessage = AsyncMock()

        await bot._process_message("Hi agent", "D12345")

        mock_chat.assert_called_once_with("Hi agent", session_id="test-session")
        bot._app.client.chat_postMessage.assert_called_once_with(
            channel="D12345", text="Hello human!"
        )

    @pytest.mark.asyncio
    async def test_session_state_persists(self, tmp_path: Path) -> None:
        """State file is created and persists across module restarts."""
        module = SlackModule(
            config={"enabled": True, "allowed_user_ids": ["U123"]},
            telemetry=_make_telemetry(),
            workspace=tmp_path,
        )
        bus = _make_bus()
        ctx = _make_module_ctx(bus, tmp_path)
        await module.startup(ctx)

        bot = module._bot
        assert bot is not None

        # Simulate 'start' text command
        bot._app = MagicMock()
        bot._app.client = MagicMock()
        bot._app.client.chat_postMessage = AsyncMock()
        await bot._handle_start("D12345", "U123")

        stored_session = bot._current_session_id
        assert stored_session is not None

        # Verify state.json was written
        state_path = tmp_path / "slack" / "state.json"
        assert state_path.exists()
        data = json.loads(state_path.read_text())
        assert data["user_id"] == "U123"
        assert data["session_id"] == stored_session

        await module.shutdown()

        # Create new module — should load persisted state
        module2 = SlackModule(
            config={"enabled": True, "allowed_user_ids": ["U123"]},
            telemetry=_make_telemetry(),
            workspace=tmp_path,
        )
        bus2 = _make_bus()
        ctx2 = _make_module_ctx(bus2, tmp_path)
        await module2.startup(ctx2)

        bot2 = module2._bot
        assert bot2 is not None
        assert bot2._user_id == "U123"
        assert bot2._current_session_id == stored_session

        await module2.shutdown()


class TestProactiveNotification:
    """Agent-driven notifications via slack_notify_user tool."""

    @pytest.mark.asyncio
    async def test_notify_user_tool_sends_to_slack(self, tmp_path: Path) -> None:
        module = SlackModule(
            config={"enabled": True, "allowed_user_ids": ["U123"]},
            telemetry=_make_telemetry(),
            workspace=tmp_path,
        )
        bus = _make_bus()
        ctx = _make_module_ctx(bus, tmp_path)
        await module.startup(ctx)

        bot = module._bot
        assert bot is not None

        # Set up bot with a stored user and mock app
        bot._user_id = "U123"
        bot._dm_channel_id = "D12345"
        mock_app = MagicMock()
        mock_app.client = MagicMock()
        mock_app.client.chat_postMessage = AsyncMock()
        bot._app = mock_app

        # Get the registered slack_notify_user tool and invoke it
        tool = ctx.tool_registry.register.call_args[0][0]
        assert tool.name == "slack_notify_user"

        result = await tool.execute(message="Found critical issue in module X")
        data = json.loads(result)
        assert data["status"] == "sent"

        mock_app.client.chat_postMessage.assert_called_once()
        sent_text = mock_app.client.chat_postMessage.call_args[1]["text"]
        assert "critical issue" in sent_text

        await module.shutdown()


class TestAuthorizationIntegration:
    """Authorization enforcement with real Module Bus."""

    @pytest.mark.asyncio
    async def test_unauthorized_message_silently_rejected(self, tmp_path: Path) -> None:
        module = SlackModule(
            config={"enabled": True, "allowed_user_ids": ["U111"]},
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

        # Unauthorized user (U999 not in allowed_user_ids [U111])
        event = {"user": "U999", "channel": "D12345", "text": "sneaky message"}
        await bot._handle_message(event)

        # chat fn should not have been called
        mock_chat.assert_not_called()

        # Verify telemetry recorded auth rejection
        telemetry_calls = bot._telemetry.record_event.call_args_list
        event_names = [c[0][0] for c in telemetry_calls]
        assert "slack:auth_rejected" in event_names

        await module.shutdown()

    @pytest.mark.asyncio
    async def test_authorized_message_proceeds(self, tmp_path: Path) -> None:
        module = SlackModule(
            config={"enabled": True, "allowed_user_ids": ["U111"]},
            telemetry=_make_telemetry(),
            workspace=tmp_path,
        )
        bus = _make_bus()
        ctx = _make_module_ctx(bus, tmp_path)
        await module.startup(ctx)

        bot = module._bot
        assert bot is not None

        # Wire up mock app and chat fn
        bot._app = MagicMock()
        bot._app.client = MagicMock()
        bot._app.client.chat_postMessage = AsyncMock()
        mock_chat = AsyncMock(return_value=MagicMock(content="response"))
        module.set_agent_chat_fn(mock_chat)

        # Authorized user
        event = {"user": "U111", "channel": "D12345", "text": "valid message"}
        await bot._handle_message(event)

        mock_chat.assert_called_once()

        await module.shutdown()


class TestTokenPrefixValidation:
    """Token prefix validation at startup."""

    @pytest.mark.asyncio
    async def test_bot_token_prefix_validated(self, tmp_path: Path) -> None:
        """Bot stays dormant if bot token has wrong prefix."""
        module = SlackModule(
            config={"enabled": True, "allowed_user_ids": ["U123"]},
            telemetry=_make_telemetry(),
            workspace=tmp_path,
        )
        bus = _make_bus()
        ctx = _make_module_ctx(bus, tmp_path)
        await module.startup(ctx)

        bot = module._bot
        assert bot is not None

        import os
        from unittest.mock import patch

        with patch.dict(
            os.environ,
            {
                "ARCAGENT_SLACK_BOT_TOKEN": "xapp-wrong",
                "ARCAGENT_SLACK_APP_TOKEN": "xapp-correct",
            },
            clear=True,
        ):
            # Create a fresh bot to test start()
            from arcagent.modules.slack.bot import SlackBot
            from arcagent.modules.slack.config import SlackConfig

            config = SlackConfig(enabled=True, allowed_user_ids=["U123"])
            fresh_bot = SlackBot(config=config, workspace=tmp_path)
            await fresh_bot.start()

            assert fresh_bot._app is None
            assert fresh_bot._running is False

        await module.shutdown()
