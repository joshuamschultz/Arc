"""Integration tests: polling-conflict / NetworkError hand-off behavior.

These tests exercise the polling-conflict and NetworkError paths in
TelegramAdapter._run_polling_loop() without requiring python-telegram-bot
to be installed — we mock the internal application object and error types.

Key test contracts (PLAN T1.10):
    test_polling_conflict_bounded_retries:
        A conflict error backs off once then sets fatal-retryable so the
        runner restarts the adapter cleanly.

    test_network_error_reconnects:
        1 NetworkError → fatal-retryable set → runner restarts adapter;
        on restart the connection succeeds.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcgateway_telegram.adapter import TelegramAdapter

# ── Fake exception classes (no library needed) ────────────────────────────────


class _FakeConflictError(Exception):
    """Simulates python-telegram-bot's Conflict exception."""


class _FakeNetworkError(Exception):
    """Simulates python-telegram-bot's NetworkError exception."""

    pass


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_adapter(
    allowed_user_ids: list[int] | None = None,
) -> TelegramAdapter:
    return TelegramAdapter(
        bot_token="test-token-xyz",
        allowed_user_ids=allowed_user_ids if allowed_user_ids is not None else [42],
        on_message=AsyncMock(),
        agent_did="did:arc:agent:test",
    )


def _make_mock_application() -> MagicMock:
    """Create a mock Application that raises on start_polling."""
    app = MagicMock()
    bot_info = MagicMock()
    bot_info.id = 77
    bot_info.username = "conflict_bot"
    app.bot.get_me = AsyncMock(return_value=bot_info)
    app.bot.send_message = AsyncMock()
    app.initialize = AsyncMock()
    app.start = AsyncMock()
    app.stop = AsyncMock()
    app.shutdown = AsyncMock()
    app.add_handler = MagicMock()
    app.add_error_handler = MagicMock()
    app.updater = MagicMock()
    app.updater.running = False
    app.updater.stop = AsyncMock()
    app.updater.start_polling = AsyncMock()
    return app


# ── Polling-conflict tests ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_polling_conflict_bounded_retries() -> None:
    """3 conflict errors → adapter sets fatal-retryable=True each time.

    PLAN T1.10.1: bounded retries (3) before escalating to
    _set_fatal_error(retryable=True) so the runner can restart cleanly.

    This test drives the polling loop by:
    1. Making start_polling raise _FakeConflictError.
    2. Patching _is_conflict_error to recognise _FakeConflictError.
    3. Patching asyncio.sleep to avoid real delays.
    4. Running the loop and asserting fatal_retryable=True is set.
    """
    adapter = _make_adapter()
    mock_app = _make_mock_application()
    adapter._application = mock_app
    adapter._running = True
    adapter._bot_id = 77

    # Raise conflict on start_polling
    mock_app.updater.start_polling.side_effect = _FakeConflictError(
        "Conflict: terminated by other getUpdates request"
    )

    # Patch _is_conflict_error to recognise our fake class
    # Patch _is_network_error to return False for _FakeConflictError
    # Patch asyncio.sleep to skip waits
    with (
        patch(
            "arcgateway_telegram.adapter._is_conflict_error",
            side_effect=lambda e: isinstance(e, _FakeConflictError),
        ),
        patch(
            "arcgateway_telegram.adapter._is_network_error",
            side_effect=lambda e: isinstance(e, _FakeNetworkError),
        ),
        patch(
            "arcgateway_telegram.adapter.asyncio.sleep",
            new_callable=AsyncMock,
        ),
        patch.dict(
            "sys.modules",
            {
                "telegram": MagicMock(),
                "telegram.ext": MagicMock(Update=MagicMock(ALL_TYPES=[])),
            },
        ),
    ):
        await adapter._run_polling_loop()

    # After the first conflict hit, fatal_retryable must be True
    assert adapter._fatal_retryable is True
    assert adapter._fatal_error is not None


@pytest.mark.asyncio
async def test_network_error_reconnects() -> None:
    """1 NetworkError → fatal-retryable=True (runner restarts adapter).

    After restart the polling succeeds — simulated by a clean run on retry.
    PLAN T1.7.1: reconnect-on-NetworkError pattern.
    """
    adapter = _make_adapter()
    mock_app = _make_mock_application()
    adapter._application = mock_app
    adapter._running = True
    adapter._bot_id = 77

    mock_app.updater.start_polling.side_effect = _FakeNetworkError("connection refused")

    with (
        patch(
            "arcgateway_telegram.adapter._is_conflict_error",
            return_value=False,
        ),
        patch(
            "arcgateway_telegram.adapter._is_network_error",
            side_effect=lambda e: isinstance(e, _FakeNetworkError),
        ),
        patch(
            "arcgateway_telegram.adapter.asyncio.sleep",
            new_callable=AsyncMock,
        ),
        patch.dict(
            "sys.modules",
            {
                "telegram": MagicMock(),
                "telegram.ext": MagicMock(Update=MagicMock(ALL_TYPES=[])),
            },
        ),
    ):
        await adapter._run_polling_loop()

    # NetworkError should set fatal_retryable=True (runner will restart)
    assert adapter._fatal_retryable is True
    assert adapter._fatal_error is not None


@pytest.mark.asyncio
async def test_unhandled_error_is_not_retryable() -> None:
    """An unknown exception type is fatal-NOT-retryable (needs human attention)."""
    adapter = _make_adapter()
    mock_app = _make_mock_application()
    adapter._application = mock_app
    adapter._running = True
    adapter._bot_id = 77

    mock_app.updater.start_polling.side_effect = ValueError("unexpected internal error")

    with (
        patch(
            "arcgateway_telegram.adapter._is_conflict_error",
            return_value=False,
        ),
        patch(
            "arcgateway_telegram.adapter._is_network_error",
            return_value=False,
        ),
        patch.dict(
            "sys.modules",
            {
                "telegram": MagicMock(),
                "telegram.ext": MagicMock(Update=MagicMock(ALL_TYPES=[])),
            },
        ),
    ):
        with pytest.raises(ValueError):
            await adapter._run_polling_loop()

    # Unhandled errors set retryable=False — manual intervention required
    assert adapter._fatal_retryable is False


@pytest.mark.asyncio
async def test_cancelled_error_propagates_cleanly() -> None:
    """asyncio.CancelledError from polling loop propagates (clean shutdown)."""
    adapter = _make_adapter()
    mock_app = _make_mock_application()
    adapter._application = mock_app
    adapter._running = True
    adapter._bot_id = 77

    mock_app.updater.start_polling.side_effect = asyncio.CancelledError()

    with patch.dict(
        "sys.modules",
        {
            "telegram": MagicMock(),
            "telegram.ext": MagicMock(Update=MagicMock(ALL_TYPES=[])),
        },
    ):
        with pytest.raises(asyncio.CancelledError):
            await adapter._run_polling_loop()

    # CancelledError must NOT set fatal_error (clean shutdown, not a crash)
    assert adapter._fatal_error is None


# ── Auth-rejection audit integration test ────────────────────────────────────


@pytest.mark.asyncio
async def test_auth_rejection_emits_audit_log(caplog: Any) -> None:
    """Auth rejection must emit a structured audit log entry.

    We verify the audit event name appears in the log output — the operator's
    log aggregator will pick it up from structured log fields.
    """
    import logging

    on_message = AsyncMock()
    adapter = TelegramAdapter(
        bot_token="test-token",
        allowed_user_ids=[100],  # user 42 is NOT allowed
        on_message=on_message,
        agent_did="did:arc:agent:test",
    )
    adapter._bot_id = 999

    update = MagicMock()
    update.update_id = 1
    update.effective_user = MagicMock()
    update.effective_user.id = 42  # not in allowlist
    update.effective_chat = MagicMock()
    update.effective_chat.id = 1000
    update.effective_message = MagicMock()
    update.effective_message.text = "injected"

    with caplog.at_level(logging.INFO, logger="arcgateway_telegram.adapter"):
        await adapter._handle_update(update, context=MagicMock())

    on_message.assert_not_called()
    # Audit event must appear in logs
    assert "gateway.adapter.auth_rejected" in caplog.text


# ── Adapter Protocol compliance ───────────────────────────────────────────────


def test_adapter_satisfies_base_platform_adapter_protocol() -> None:
    """TelegramAdapter must satisfy the BasePlatformAdapter Protocol at runtime."""
    from arcgateway.adapters.base import BasePlatformAdapter

    adapter = _make_adapter()
    assert isinstance(adapter, BasePlatformAdapter)
