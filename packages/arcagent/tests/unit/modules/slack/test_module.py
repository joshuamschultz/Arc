"""Unit tests for SlackModule lifecycle — SPEC-011 Phase 1 + 3."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.modules.slack import SlackModule
from tests.unit.modules.slack.conftest import make_ctx


def _make_module(tmp_path: Path) -> SlackModule:
    """Create a SlackModule with mock dependencies."""
    telemetry = MagicMock()
    return SlackModule(
        config={"enabled": True, "allowed_user_ids": ["U123"]},
        telemetry=telemetry,
        workspace=tmp_path,
    )


class TestModuleProtocol:
    def test_has_name(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        assert module.name == "slack"

    def test_has_startup(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        assert hasattr(module, "startup")
        assert callable(module.startup)

    def test_has_shutdown(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        assert hasattr(module, "shutdown")
        assert callable(module.shutdown)

    def test_has_set_agent_chat_fn(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        assert hasattr(module, "set_agent_chat_fn")
        assert callable(module.set_agent_chat_fn)


class TestModuleConstruction:
    def test_default_config(self, tmp_path: Path) -> None:
        """Module can be constructed with no config (defaults)."""
        module = SlackModule(workspace=tmp_path)
        assert module.name == "slack"
        assert module._config.enabled is False

    def test_config_from_dict(self, tmp_path: Path) -> None:
        """Config dict is validated via SlackConfig."""
        module = SlackModule(
            config={"enabled": True, "allowed_user_ids": ["U42"]},
            workspace=tmp_path,
        )
        assert module._config.enabled is True
        assert module._config.allowed_user_ids == ["U42"]


class TestModuleStartup:
    @pytest.mark.asyncio
    async def test_startup_subscribes_to_shutdown(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        subscribed_events = [
            call.args[0] for call in ctx.bus.subscribe.call_args_list
        ]
        assert "agent:shutdown" in subscribed_events

    @pytest.mark.asyncio
    async def test_startup_subscribes_to_agent_ready(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        subscribed_events = [
            call.args[0] for call in ctx.bus.subscribe.call_args_list
        ]
        assert "agent:ready" in subscribed_events

    @pytest.mark.asyncio
    async def test_startup_subscribes_to_schedule_failed(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        subscribed_events = [
            call.args[0] for call in ctx.bus.subscribe.call_args_list
        ]
        assert "schedule:failed" in subscribed_events

    @pytest.mark.asyncio
    async def test_startup_registers_slack_notify_user_tool(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        ctx.tool_registry.register.assert_called_once()
        tool = ctx.tool_registry.register.call_args[0][0]
        assert tool.name == "slack_notify_user"

    @pytest.mark.asyncio
    async def test_startup_stores_bus_reference(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        assert module._bus is ctx.bus

    @pytest.mark.asyncio
    async def test_startup_creates_bot(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        assert module._bot is not None

    @pytest.mark.asyncio
    async def test_startup_calls_bot_start(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)

        with patch(
            "arcagent.modules.slack.bot.SlackBot"
        ) as MockBot:
            mock_bot = MagicMock()
            mock_bot.start = AsyncMock()
            MockBot.return_value = mock_bot

            await module.startup(ctx)
            mock_bot.start.assert_called_once()


class TestModuleShutdown:
    @pytest.mark.asyncio
    async def test_double_shutdown_is_safe(self, tmp_path: Path) -> None:
        """Calling shutdown() twice should not raise."""
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        await module.shutdown()
        await module.shutdown()  # second call is a no-op

    @pytest.mark.asyncio
    async def test_shutdown_stops_bot(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)

        with patch(
            "arcagent.modules.slack.bot.SlackBot"
        ) as MockBot:
            mock_bot = MagicMock()
            mock_bot.start = AsyncMock()
            mock_bot.stop = AsyncMock()
            MockBot.return_value = mock_bot

            await module.startup(ctx)
            await module.shutdown()
            mock_bot.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_clears_bot_reference(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)

        with patch(
            "arcagent.modules.slack.bot.SlackBot"
        ) as MockBot:
            mock_bot = MagicMock()
            mock_bot.start = AsyncMock()
            mock_bot.stop = AsyncMock()
            MockBot.return_value = mock_bot

            await module.startup(ctx)
            assert module._bot is not None
            await module.shutdown()
            assert module._bot is None


class TestSetAgentChatFn:
    @pytest.mark.asyncio
    async def test_propagates_to_bot(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)

        with patch(
            "arcagent.modules.slack.bot.SlackBot"
        ) as MockBot:
            mock_bot = MagicMock()
            mock_bot.start = AsyncMock()
            mock_bot.set_agent_chat_fn = MagicMock()
            MockBot.return_value = mock_bot

            await module.startup(ctx)
            dummy_fn = AsyncMock()
            module.set_agent_chat_fn(dummy_fn)
            mock_bot.set_agent_chat_fn.assert_called_once_with(dummy_fn)

    def test_noop_when_no_bot(self, tmp_path: Path) -> None:
        """set_agent_chat_fn before startup is a no-op (no crash)."""
        module = _make_module(tmp_path)
        module.set_agent_chat_fn(AsyncMock())  # should not raise


class TestScheduleEventHandlers:
    @pytest.mark.asyncio
    async def test_schedule_failed_sends_notification(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)

        with patch(
            "arcagent.modules.slack.bot.SlackBot"
        ) as MockBot:
            mock_bot = MagicMock()
            mock_bot.start = AsyncMock()
            mock_bot.send_notification = AsyncMock()
            MockBot.return_value = mock_bot

            await module.startup(ctx)

            event = MagicMock()
            event.data = {"error": "timeout", "schedule_name": "backup"}
            await module._on_schedule_failed(event)

            mock_bot.send_notification.assert_called_once()
            msg = mock_bot.send_notification.call_args[0][0]
            assert "timeout" in msg
            assert "failed" in msg.lower()

    @pytest.mark.asyncio
    async def test_schedule_failed_noop_when_no_bot(self, tmp_path: Path) -> None:
        """Schedule failed is a no-op if bot is not running."""
        module = _make_module(tmp_path)
        event = MagicMock()
        await module._on_schedule_failed(event)  # should not raise


class TestNotifyUserTool:
    @pytest.mark.asyncio
    async def test_notify_user_sends_to_slack(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)

        with patch(
            "arcagent.modules.slack.bot.SlackBot"
        ) as MockBot:
            mock_bot = MagicMock()
            mock_bot.start = AsyncMock()
            mock_bot.send_notification = AsyncMock()
            MockBot.return_value = mock_bot

            await module.startup(ctx)

            tool = ctx.tool_registry.register.call_args[0][0]
            result = await tool.execute(message="Important finding: X is broken")

            mock_bot.send_notification.assert_called_once_with(
                "Important finding: X is broken"
            )
            import json

            data = json.loads(result)
            assert data["status"] == "sent"

    @pytest.mark.asyncio
    async def test_notify_user_rejects_empty_message(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)

        with patch(
            "arcagent.modules.slack.bot.SlackBot"
        ) as MockBot:
            mock_bot = MagicMock()
            mock_bot.start = AsyncMock()
            MockBot.return_value = mock_bot

            await module.startup(ctx)

            tool = ctx.tool_registry.register.call_args[0][0]
            result = await tool.execute(message="")

            import json

            data = json.loads(result)
            assert "error" in data


class TestBusEventEmission:
    @pytest.mark.asyncio
    async def test_startup_emits_module_started(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)

        emit_calls = [c[0][0] for c in ctx.bus.emit.call_args_list]
        assert "slack:module_started" in emit_calls

    @pytest.mark.asyncio
    async def test_shutdown_emits_module_stopped(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)
        await module.shutdown()

        emit_calls = [c[0][0] for c in ctx.bus.emit.call_args_list]
        assert "slack:module_stopped" in emit_calls

    @pytest.mark.asyncio
    async def test_no_token_in_events(self, tmp_path: Path) -> None:
        """Verify no bot/app token appears in any emitted event data."""
        module = _make_module(tmp_path)
        ctx = make_ctx(tmp_path)
        await module.startup(ctx)

        for call in ctx.bus.emit.call_args_list:
            event_data = call[0][1] if len(call[0]) > 1 else {}
            data_str = str(event_data)
            assert "bot_token" not in data_str.lower()
            assert "app_token" not in data_str.lower()
            assert "ARCAGENT_SLACK" not in data_str
