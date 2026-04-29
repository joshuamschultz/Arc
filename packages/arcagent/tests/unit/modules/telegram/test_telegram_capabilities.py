"""SPEC-021 Task 3.5 — telegram module decorator-form tests.

The new ``capabilities.py`` exposes:

  * 1 ``@background_task`` (telegram_poll)
  * 1 ``@tool`` (notify_user)
  * 3 ``@hook``-decorated functions (agent:shutdown,
    schedule:completed, schedule:failed)

This file verifies:

  1. All five capabilities register via :class:`CapabilityLoader`
     against the telegram module directory.
  2. Hook events match the SPEC-021 PLAN task 3.5 requirements.
  3. ``notify_user`` delegates to the bot and rejects empty input.
  4. ``on_schedule_failed`` calls ``bot.send_notification`` with the
     failure error message.
  5. The poll loop drains cleanly on cancellation (R-062).

Legacy :class:`TelegramModule` tests in ``test_module.py`` continue
to verify the wrapper-class behavior unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.core.capability_loader import CapabilityLoader
from arcagent.core.capability_registry import CapabilityRegistry
from arcagent.modules.telegram import _runtime


@pytest.fixture(autouse=True)
def _reset_runtime() -> Iterator[None]:
    _runtime.reset()
    yield
    _runtime.reset()


@pytest.fixture
def configured(tmp_path: Path) -> Iterator[MagicMock]:
    """Configure runtime with a mocked TelegramBot.

    Returns the mock bot so tests can assert delegated calls.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mock_bot = MagicMock()
    mock_bot.start = AsyncMock()
    mock_bot.stop = AsyncMock()
    mock_bot.send_notification = AsyncMock()

    with patch("arcagent.modules.telegram._runtime.TelegramBot") as MockBot:
        MockBot.return_value = mock_bot
        _runtime.configure(
            config={"enabled": True, "allowed_chat_ids": [42]},
            telemetry=MagicMock(),
            workspace=workspace,
        )
        yield mock_bot


@pytest.mark.asyncio
class TestLoaderRegistration:
    async def test_all_capabilities_register(self, configured: MagicMock) -> None:
        """All five capabilities are picked up by CapabilityLoader.

        Uses the ``configured`` fixture so the background task spawned
        on registration sees a mocked TelegramBot and does not touch
        the real network.
        """
        from arcagent.modules.telegram import capabilities as tg_caps

        module_dir = Path(tg_caps.__file__).parent
        # Loader scans .py files; only capabilities.py carries stamps
        # in this directory (bot.py, config.py, _runtime.py do not).
        reg = CapabilityRegistry()
        loader = CapabilityLoader(scan_roots=[("telegram", module_dir)], registry=reg)
        await loader.scan_and_register()

        # Tool
        tool_entry = await reg.get_tool("notify_user")
        assert tool_entry is not None

        # Hooks (3)
        shutdown_hooks = await reg.get_hooks("agent:shutdown")
        completed_hooks = await reg.get_hooks("schedule:completed")
        failed_hooks = await reg.get_hooks("schedule:failed")
        assert any(h.meta.name == "on_agent_shutdown" for h in shutdown_hooks)
        assert any(h.meta.name == "on_schedule_completed" for h in completed_hooks)
        assert any(h.meta.name == "on_schedule_failed" for h in failed_hooks)

        # Background task
        task_entry = await reg.get_task("telegram_poll")
        assert task_entry is not None
        # Drain the live task so the test loop exits cleanly (R-062).
        if task_entry.task is not None:
            task_entry.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task_entry.task

    async def test_background_task_interval_metadata(self) -> None:
        from arcagent.modules.telegram.capabilities import telegram_poll

        meta = telegram_poll._arc_capability_meta  # type: ignore[attr-defined]
        assert meta.kind == "background_task"
        assert meta.interval > 0


@pytest.mark.asyncio
class TestNotifyUserTool:
    async def test_sends_via_bot(self, configured: MagicMock) -> None:
        from arcagent.modules.telegram.capabilities import notify_user

        result = await notify_user(message="Important: X is broken")
        configured.send_notification.assert_awaited_once_with("Important: X is broken")
        data = json.loads(result)
        assert data["status"] == "sent"
        assert data["length"] == len("Important: X is broken")

    async def test_rejects_empty_message(self, configured: MagicMock) -> None:
        from arcagent.modules.telegram.capabilities import notify_user

        result = await notify_user(message="")
        configured.send_notification.assert_not_called()
        data = json.loads(result)
        assert "error" in data


@pytest.mark.asyncio
class TestHooks:
    async def test_agent_shutdown_stops_bot(self, configured: MagicMock) -> None:
        from arcagent.modules.telegram.capabilities import on_agent_shutdown

        await on_agent_shutdown(SimpleNamespace(data={}))
        configured.stop.assert_awaited_once()

    async def test_schedule_failed_notifies(self, configured: MagicMock) -> None:
        from arcagent.modules.telegram.capabilities import on_schedule_failed

        ctx = SimpleNamespace(data={"error": "timeout", "schedule_name": "backup"})
        await on_schedule_failed(ctx)
        configured.send_notification.assert_awaited_once()
        sent = configured.send_notification.call_args[0][0]
        assert "timeout" in sent
        assert "failed" in sent

    async def test_schedule_completed_does_not_forward(self, configured: MagicMock) -> None:
        """schedule:completed must not auto-send a Telegram message.

        The agent decides via ``notify_user`` what completed work is
        worth surfacing; this hook only exists as an observability point.
        """
        from arcagent.modules.telegram.capabilities import on_schedule_completed

        ctx = SimpleNamespace(data={"schedule_name": "backup", "result": "ok"})
        await on_schedule_completed(ctx)
        configured.send_notification.assert_not_called()


@pytest.mark.asyncio
class TestBackgroundTaskDrain:
    async def test_poll_drains_on_cancellation(self, configured: MagicMock) -> None:
        """Cancellation of telegram_poll must call bot.stop() (R-062)."""
        from arcagent.modules.telegram.capabilities import telegram_poll

        task = asyncio.create_task(telegram_poll(None))
        # Yield to let the bot.start() coroutine schedule.
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        configured.start.assert_awaited_once()
        configured.stop.assert_awaited_once()


@pytest.mark.asyncio
class TestRuntimeContract:
    async def test_unconfigured_raises(self) -> None:
        from arcagent.modules.telegram.capabilities import notify_user

        with pytest.raises(RuntimeError, match="before runtime is configured"):
            await notify_user(message="hello")
