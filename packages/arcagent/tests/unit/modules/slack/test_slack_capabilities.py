"""SPEC-021 Task 3.6 — slack module decorator-form tests.

The new ``capabilities.py`` exposes:

  * a ``@capability(name="slack")`` class with ``setup`` / ``teardown``
  * a module-level ``@tool slack_notify_user``
  * three ``@hook`` functions (``agent:ready``, ``agent:shutdown``,
    ``schedule:failed``)

This file verifies:

  1. The :class:`CapabilityLoader` registers the class as a
     :class:`LifecycleEntry`, the tool, and all three hooks.
  2. ``setup`` instantiates and starts a :class:`SlackBot`;
     ``teardown`` stops it. WebSocket I/O is mocked.
  3. ``slack_notify_user`` calls ``bot.send_notification``.
  4. ``bind_agent_chat_fn`` propagates the chat fn into the bot.
  5. ``notify_schedule_failed`` sends a failure notification.

Legacy :class:`SlackModule` tests in ``test_module.py`` continue to
verify behaviour at the wrapper level.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.core.capability_loader import CapabilityLoader
from arcagent.core.capability_registry import (
    CapabilityRegistry,
    LifecycleEntry,
)
from arcagent.modules.slack import _runtime


@pytest.fixture(autouse=True)
def _reset_runtime() -> None:
    _runtime.reset()


@pytest.fixture
def configured(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _runtime.configure(
        config={"enabled": True, "allowed_user_ids": ["U123"]},
        workspace=workspace,
    )
    return workspace


@pytest.mark.asyncio
class TestLoaderRegistration:
    async def test_capability_class_registers_as_lifecycle_entry(self) -> None:
        from arcagent.modules.slack import capabilities as slack_caps

        module_dir = Path(slack_caps.__file__).parent
        reg = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=[("slack", module_dir)], registry=reg)
        await loader.scan_and_register()

        entry = await reg.get_capability("slack")
        assert entry is not None
        assert isinstance(entry, LifecycleEntry)
        assert entry.meta.name == "slack"

    async def test_tool_registers(self) -> None:
        from arcagent.modules.slack import capabilities as slack_caps

        module_dir = Path(slack_caps.__file__).parent
        reg = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=[("slack", module_dir)], registry=reg)
        await loader.scan_and_register()

        tool_entry = await reg.get_tool("slack_notify_user")
        assert tool_entry is not None
        assert tool_entry.meta.name == "slack_notify_user"

    async def test_three_hooks_register(self) -> None:
        from arcagent.modules.slack import capabilities as slack_caps

        module_dir = Path(slack_caps.__file__).parent
        reg = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=[("slack", module_dir)], registry=reg)
        await loader.scan_and_register()

        ready_hooks = await reg.get_hooks("agent:ready")
        shutdown_hooks = await reg.get_hooks("agent:shutdown")
        failed_hooks = await reg.get_hooks("schedule:failed")

        assert any(h.meta.name == "bind_agent_chat_fn" for h in ready_hooks)
        assert any(h.meta.name == "stop_slack_bot" for h in shutdown_hooks)
        assert any(h.meta.name == "notify_schedule_failed" for h in failed_hooks)


@pytest.mark.asyncio
class TestCapabilityLifecycle:
    async def test_setup_instantiates_and_starts_bot(self, configured: Path) -> None:
        from arcagent.modules.slack.capabilities import SlackCapability

        with patch("arcagent.modules.slack.capabilities.SlackBot") as MockBot:
            mock_bot = MagicMock()
            mock_bot.start = AsyncMock()
            MockBot.return_value = mock_bot

            cap = SlackCapability()
            await cap.setup(ctx=None)

            mock_bot.start.assert_called_once()
            assert _runtime.state().bot is mock_bot

    async def test_teardown_stops_bot_and_clears_state(self, configured: Path) -> None:
        from arcagent.modules.slack.capabilities import SlackCapability

        with patch("arcagent.modules.slack.capabilities.SlackBot") as MockBot:
            mock_bot = MagicMock()
            mock_bot.start = AsyncMock()
            mock_bot.stop = AsyncMock()
            MockBot.return_value = mock_bot

            cap = SlackCapability()
            await cap.setup(ctx=None)
            await cap.teardown()

            mock_bot.stop.assert_called_once()
            assert _runtime.state().bot is None

    async def test_setup_is_idempotent(self, configured: Path) -> None:
        from arcagent.modules.slack.capabilities import SlackCapability

        with patch("arcagent.modules.slack.capabilities.SlackBot") as MockBot:
            mock_bot = MagicMock()
            mock_bot.start = AsyncMock()
            MockBot.return_value = mock_bot

            cap = SlackCapability()
            await cap.setup(ctx=None)
            await cap.setup(ctx=None)

            mock_bot.start.assert_called_once()

    async def test_teardown_is_idempotent_when_no_bot(self, configured: Path) -> None:
        from arcagent.modules.slack.capabilities import SlackCapability

        cap = SlackCapability()
        await cap.teardown()  # must not raise


@pytest.mark.asyncio
class TestNotifyUserTool:
    async def test_sends_via_bot(self, configured: Path) -> None:
        from arcagent.modules.slack.capabilities import slack_notify_user

        mock_bot = MagicMock()
        mock_bot.send_notification = AsyncMock()
        _runtime.state().bot = mock_bot

        result = await slack_notify_user(message="ship it")
        mock_bot.send_notification.assert_called_once_with("ship it")
        import json

        payload = json.loads(result)
        assert payload["status"] == "sent"
        assert payload["length"] == len("ship it")

    async def test_rejects_empty_message(self, configured: Path) -> None:
        from arcagent.modules.slack.capabilities import slack_notify_user

        result = await slack_notify_user(message="")
        import json

        payload = json.loads(result)
        assert "error" in payload

    async def test_errors_when_bot_absent(self, configured: Path) -> None:
        from arcagent.modules.slack.capabilities import slack_notify_user

        _runtime.state().bot = None
        result = await slack_notify_user(message="hello")
        import json

        payload = json.loads(result)
        assert "not running" in payload["error"]


@pytest.mark.asyncio
class TestHooks:
    async def test_bind_agent_chat_fn_propagates(self, configured: Path) -> None:
        from arcagent.modules.slack.capabilities import bind_agent_chat_fn

        mock_bot = MagicMock()
        mock_bot.set_agent_chat_fn = MagicMock()
        _runtime.state().bot = mock_bot

        chat_fn = AsyncMock()
        ctx = SimpleNamespace(data={"chat_fn": chat_fn})
        await bind_agent_chat_fn(ctx)
        mock_bot.set_agent_chat_fn.assert_called_once_with(chat_fn)

    async def test_bind_agent_chat_fn_noop_when_no_bot(self, configured: Path) -> None:
        from arcagent.modules.slack.capabilities import bind_agent_chat_fn

        ctx = SimpleNamespace(data={"chat_fn": AsyncMock()})
        await bind_agent_chat_fn(ctx)  # must not raise

    async def test_stop_slack_bot_calls_stop(self, configured: Path) -> None:
        from arcagent.modules.slack.capabilities import stop_slack_bot

        mock_bot = MagicMock()
        mock_bot.stop = AsyncMock()
        _runtime.state().bot = mock_bot

        await stop_slack_bot(SimpleNamespace(data={}))
        mock_bot.stop.assert_called_once()
        assert _runtime.state().bot is None

    async def test_stop_slack_bot_noop_when_no_bot(self, configured: Path) -> None:
        from arcagent.modules.slack.capabilities import stop_slack_bot

        await stop_slack_bot(SimpleNamespace(data={}))  # must not raise

    async def test_notify_schedule_failed_sends(self, configured: Path) -> None:
        from arcagent.modules.slack.capabilities import notify_schedule_failed

        mock_bot = MagicMock()
        mock_bot.send_notification = AsyncMock()
        _runtime.state().bot = mock_bot

        ctx = SimpleNamespace(data={"error": "timeout", "schedule_name": "backup"})
        await notify_schedule_failed(ctx)
        mock_bot.send_notification.assert_called_once()
        msg = mock_bot.send_notification.call_args[0][0]
        assert "timeout" in msg
        assert "failed" in msg.lower()

    async def test_notify_schedule_failed_noop_when_no_bot(self, configured: Path) -> None:
        from arcagent.modules.slack.capabilities import notify_schedule_failed

        ctx = SimpleNamespace(data={"error": "timeout"})
        await notify_schedule_failed(ctx)  # must not raise


@pytest.mark.asyncio
class TestRuntimeContract:
    async def test_unconfigured_raises(self) -> None:
        from arcagent.modules.slack.capabilities import slack_notify_user

        with pytest.raises(RuntimeError, match="before runtime is configured"):
            await slack_notify_user(message="hello")
