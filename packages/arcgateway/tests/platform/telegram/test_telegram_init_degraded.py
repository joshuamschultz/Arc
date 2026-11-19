"""A Telegram outage at startup degrades the adapter; it never kills the process.

Regression for the overnight outage: with the network down at boot, the bounded
init retry raised out of ``connect()``, through the arcui lifespan, and systemd
restarted the whole stack into the same failure for hours. The contract now:

    retryable (network / timeout) → connect() returns, health() reports
        degraded with the last error, init keeps retrying in the background
        (capped backoff, no attempt limit) and starts polling once it succeeds;
    non-retryable (bad token, auth rejected) → connect() still raises.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcgateway.adapters.telegram import adapter as telegram_adapter
from arcgateway.adapters.telegram.adapter import TelegramAdapter


class NetworkError(Exception):
    """Named like python-telegram-bot's ``telegram.error.NetworkError``."""


class InvalidTokenError(Exception):
    """Named like python-telegram-bot's ``telegram.error.InvalidToken``."""


def _mock_application() -> MagicMock:
    app = MagicMock()
    bot_info = MagicMock()
    bot_info.id = 99
    bot_info.username = "test_bot"
    app.bot.get_me = AsyncMock(return_value=bot_info)
    app.bot.set_my_commands = AsyncMock()
    app.initialize = AsyncMock()
    app.start = AsyncMock()
    app.stop = AsyncMock()
    app.shutdown = AsyncMock()
    app.updater = MagicMock()
    app.updater.running = False
    app.updater.stop = AsyncMock()
    app.updater.start_polling = AsyncMock()
    return app


@pytest.fixture
def mock_app(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Install a fake ``telegram.ext.Application`` and make backoff instant."""
    app = _mock_application()
    builder = MagicMock()
    builder.token.return_value = builder
    builder.concurrent_updates.return_value = builder
    builder.build.return_value = app
    fake_ext = MagicMock()
    fake_ext.Application.builder.return_value = builder
    monkeypatch.setitem(sys.modules, "telegram", MagicMock())
    monkeypatch.setitem(sys.modules, "telegram.ext", fake_ext)
    monkeypatch.setattr(telegram_adapter, "_network_backoff", lambda _attempt: 0.0)
    return app


def _adapter() -> TelegramAdapter:
    return TelegramAdapter(
        bot_token="test-token",
        allowed_user_ids=[42],
        on_message=AsyncMock(),
        agent_did="did:arc:agent:test",
    )


async def _until(predicate: Any, timeout: float = 2.0) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout)


@pytest.mark.asyncio
async def test_persistent_network_error_at_init_does_not_raise(mock_app: MagicMock) -> None:
    mock_app.initialize.side_effect = NetworkError("network is unreachable")
    adapter = _adapter()
    try:
        await asyncio.wait_for(adapter.connect(), timeout=2.0)

        health = adapter.health()
        assert health["state"] == "degraded"
        assert "network is unreachable" in str(health["last_error"])
        # Retrying continues past the old 5-attempt budget.
        await _until(lambda: mock_app.initialize.await_count > 10)
        mock_app.updater.start_polling.assert_not_awaited()
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_background_retry_connects_and_starts_polling(mock_app: MagicMock) -> None:
    mock_app.initialize.side_effect = [NetworkError("dns failure")] * 7 + [None]
    adapter = _adapter()
    try:
        await adapter.connect()
        await _until(lambda: adapter.health()["state"] == "connected")

        assert adapter.health()["last_error"] is None
        assert adapter._bot_id == 99
        await _until(lambda: mock_app.updater.start_polling.await_count == 1)
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_degraded_init_does_not_signal_event_loop_death(mock_app: MagicMock) -> None:
    """The adapter owns its init retry; the runner's reconnect watcher must not race it."""
    mock_app.initialize.side_effect = NetworkError("down")
    adapter = _adapter()
    try:
        await adapter.connect()
        waiter = asyncio.ensure_future(adapter.wait_closed())
        await asyncio.sleep(0.05)
        assert not waiter.done()
        waiter.cancel()
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_auth_error_at_init_stays_fatal(mock_app: MagicMock) -> None:
    mock_app.initialize.side_effect = InvalidTokenError("The token was rejected by the server.")
    adapter = _adapter()

    with pytest.raises(InvalidTokenError):
        await adapter.connect()

    health = adapter.health()
    assert health["state"] == "failed"
    assert "rejected" in str(health["last_error"])
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_auth_error_during_background_retry_marks_failed(mock_app: MagicMock) -> None:
    mock_app.initialize.side_effect = [NetworkError("down"), InvalidTokenError("revoked")]
    adapter = _adapter()
    try:
        await adapter.connect()
        retryable, error = await asyncio.wait_for(adapter.wait_closed(), timeout=2.0)

        assert retryable is False
        assert isinstance(error, InvalidTokenError)
        assert adapter.health()["state"] == "failed"
        mock_app.updater.start_polling.assert_not_awaited()
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_disconnect_cancels_background_init_retry(mock_app: MagicMock) -> None:
    mock_app.initialize.side_effect = NetworkError("down")
    adapter = _adapter()
    await adapter.connect()
    await _until(lambda: mock_app.initialize.await_count >= 2)

    await adapter.disconnect()
    calls = mock_app.initialize.await_count
    await asyncio.sleep(0.05)

    assert mock_app.initialize.await_count == calls
    assert adapter.health()["state"] == "disconnected"
    current = asyncio.current_task()
    leftovers = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    assert not [t for t in leftovers if t.get_name().startswith("telegram:")]
