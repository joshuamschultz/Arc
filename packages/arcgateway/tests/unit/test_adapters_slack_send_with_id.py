"""Regression tests for SlackAdapter.send_with_id(), edit_message(), and _DedupStore.

Wave-0 changed record_or_skip and sweep_expired to sync methods. These tests lock in:
- send_with_id() returns the Slack message ts as str
- send_with_id() raises RuntimeError when not connected
- send_with_id() falls back to message text when split gives empty list
- edit_message() calls chat_update with correct args
- edit_message() raises on API failure
- edit_message() raises when not connected
- _DedupStore.record_or_skip() is synchronous (no await needed)
- _DedupStore.sweep_expired() is synchronous and returns count
- _DedupStore dedup: second call with same event_id returns True
- _DedupStore sweep: clears old rows, keeps recent ones
- _classify_prefix() helper covers all expected classes
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcgateway.adapters.slack import (
    SlackAdapter,
    _classify_prefix,
    _DedupStore,
)
from arcgateway.delivery import DeliveryTarget

# ---------------------------------------------------------------------------
# Test helpers (mirrored from test_adapters_slack.py)
# ---------------------------------------------------------------------------


def _make_adapter(allowed_user_ids: list[str] | None = None) -> SlackAdapter:
    adapter = SlackAdapter(
        bot_token="xoxb-valid-token",
        app_token="xapp-valid-token",
        allowed_user_ids=allowed_user_ids if allowed_user_ids is not None else ["U123"],
        on_message=AsyncMock(),
        dedup_db_path=None,
    )
    return adapter


def _build_mock_bolt() -> tuple[MagicMock, MagicMock, MagicMock, MagicMock]:
    mock_client = MagicMock()
    mock_client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "1234567890.123456"})
    mock_client.chat_update = AsyncMock(return_value={"ok": True})

    mock_app = MagicMock()
    mock_app.client = mock_client
    mock_app.event = MagicMock(return_value=lambda fn: fn)

    mock_app_cls = MagicMock(return_value=mock_app)

    mock_handler = MagicMock()
    mock_handler.connect_async = AsyncMock()
    mock_handler.close_async = AsyncMock()
    mock_handler_cls = MagicMock(return_value=mock_handler)

    return mock_app, mock_app_cls, mock_handler, mock_handler_cls


def _make_bolt_modules(
    mock_app_cls: MagicMock,
    mock_handler_cls: MagicMock,
) -> dict[str, Any]:
    mock_async_app_mod = MagicMock()
    mock_async_app_mod.AsyncApp = mock_app_cls

    mock_handler_mod = MagicMock()
    mock_handler_mod.AsyncSocketModeHandler = mock_handler_cls

    mock_adapter_mod = MagicMock()
    mock_adapter_mod.socket_mode = MagicMock()
    mock_adapter_mod.socket_mode.async_handler = mock_handler_mod

    mock_bolt = MagicMock()
    mock_bolt.async_app = mock_async_app_mod
    mock_bolt.adapter = mock_adapter_mod

    return {
        "slack_bolt": mock_bolt,
        "slack_bolt.async_app": mock_async_app_mod,
        "slack_bolt.adapter": mock_adapter_mod,
        "slack_bolt.adapter.socket_mode": mock_adapter_mod.socket_mode,
        "slack_bolt.adapter.socket_mode.async_handler": mock_handler_mod,
    }


# ---------------------------------------------------------------------------
# send_with_id() tests
# ---------------------------------------------------------------------------


class TestSendWithId:
    async def test_send_with_id_returns_ts(self) -> None:
        """send_with_id() returns the Slack message timestamp (ts)."""
        mock_app, mock_app_cls, mock_handler, mock_handler_cls = _build_mock_bolt()
        modules = _make_bolt_modules(mock_app_cls, mock_handler_cls)

        # chat_postMessage returns a dict with 'ts'
        mock_app.client.chat_postMessage = AsyncMock(
            return_value={"ok": True, "ts": "1700000000.000001"}
        )

        with patch.dict(sys.modules, modules):
            adapter = _make_adapter()
            await adapter.connect()

            target = DeliveryTarget.parse("slack:D456")
            result = await adapter.send_with_id(target, "hello from slack")

        assert result == "1700000000.000001"

    async def test_send_with_id_raises_when_not_connected(self) -> None:
        """send_with_id() before connect() raises RuntimeError."""
        adapter = _make_adapter()
        target = DeliveryTarget.parse("slack:D456")
        with pytest.raises(RuntimeError, match="connect"):
            await adapter.send_with_id(target, "oops")

    async def test_send_with_id_returns_none_when_ts_absent(self) -> None:
        """send_with_id() returns None when API response has no 'ts' key."""
        mock_app, mock_app_cls, mock_handler, mock_handler_cls = _build_mock_bolt()
        modules = _make_bolt_modules(mock_app_cls, mock_handler_cls)

        # No 'ts' in response
        mock_app.client.chat_postMessage = AsyncMock(return_value={"ok": True})

        with patch.dict(sys.modules, modules):
            adapter = _make_adapter()
            await adapter.connect()

            target = DeliveryTarget.parse("slack:D456")
            result = await adapter.send_with_id(target, "no ts response")

        assert result is None

    async def test_send_with_id_uses_first_chunk_only(self) -> None:
        """send_with_id() truncates long messages to the first chunk only."""
        mock_app, mock_app_cls, mock_handler, mock_handler_cls = _build_mock_bolt()
        modules = _make_bolt_modules(mock_app_cls, mock_handler_cls)
        mock_app.client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "ts-001"})

        with patch.dict(sys.modules, modules):
            adapter = _make_adapter()
            await adapter.connect()

            long_message = "X" * 5000  # exceeds 4000-char Slack limit
            target = DeliveryTarget.parse("slack:D456")
            await adapter.send_with_id(target, long_message)

        # Must issue exactly ONE call (first chunk only)
        assert mock_app.client.chat_postMessage.call_count == 1

    async def test_send_with_id_object_response_ts(self) -> None:
        """send_with_id() handles response objects with .get() method (not only dicts)."""
        mock_app, mock_app_cls, mock_handler, mock_handler_cls = _build_mock_bolt()
        modules = _make_bolt_modules(mock_app_cls, mock_handler_cls)

        # Simulate a slack SDK SlackResponse object with .get()
        mock_response = MagicMock()
        mock_response.get = MagicMock(return_value="response_obj_ts")
        mock_app.client.chat_postMessage = AsyncMock(return_value=mock_response)

        with patch.dict(sys.modules, modules):
            adapter = _make_adapter()
            await adapter.connect()
            target = DeliveryTarget.parse("slack:D456")
            result = await adapter.send_with_id(target, "obj response")

        assert result == "response_obj_ts"


# ---------------------------------------------------------------------------
# edit_message() tests
# ---------------------------------------------------------------------------


class TestEditMessage:
    async def test_edit_message_calls_chat_update(self) -> None:
        """edit_message() calls Slack's chat.update API with correct args."""
        mock_app, mock_app_cls, mock_handler, mock_handler_cls = _build_mock_bolt()
        modules = _make_bolt_modules(mock_app_cls, mock_handler_cls)

        with patch.dict(sys.modules, modules):
            adapter = _make_adapter()
            await adapter.connect()

            target = DeliveryTarget.parse("slack:D456")
            await adapter.edit_message(target, "1700000000.000001", "updated text")

            mock_app.client.chat_update.assert_called_once_with(
                channel="D456",
                ts="1700000000.000001",
                text="updated text",
            )

    async def test_edit_message_raises_when_not_connected(self) -> None:
        """edit_message() before connect() raises RuntimeError."""
        adapter = _make_adapter()
        target = DeliveryTarget.parse("slack:D456")
        with pytest.raises(RuntimeError, match="connect"):
            await adapter.edit_message(target, "ts", "new text")

    async def test_edit_message_truncates_to_max_length(self) -> None:
        """edit_message() truncates new_text to 4000 chars before calling chat.update."""
        mock_app, mock_app_cls, mock_handler, mock_handler_cls = _build_mock_bolt()
        modules = _make_bolt_modules(mock_app_cls, mock_handler_cls)

        with patch.dict(sys.modules, modules):
            adapter = _make_adapter()
            await adapter.connect()

            target = DeliveryTarget.parse("slack:D456")
            long_text = "Y" * 5000
            await adapter.edit_message(target, "ts-001", long_text)

            call_kwargs = mock_app.client.chat_update.call_args.kwargs
            assert len(call_kwargs["text"]) == 4000

    async def test_edit_message_raises_on_api_failure(self) -> None:
        """edit_message() re-raises when chat.update API call fails."""
        mock_app, mock_app_cls, mock_handler, mock_handler_cls = _build_mock_bolt()
        modules = _make_bolt_modules(mock_app_cls, mock_handler_cls)

        mock_app.client.chat_update = AsyncMock(side_effect=RuntimeError("API error"))

        with patch.dict(sys.modules, modules):
            adapter = _make_adapter()
            await adapter.connect()

            target = DeliveryTarget.parse("slack:D456")
            with pytest.raises(RuntimeError, match="API error"):
                await adapter.edit_message(target, "ts-fail", "new text")


# ---------------------------------------------------------------------------
# _DedupStore tests (sync API — record_or_skip, sweep_expired)
# ---------------------------------------------------------------------------


class TestDedupStore:
    def test_record_or_skip_first_call_returns_false(self) -> None:
        """record_or_skip returns False on first sight (not a duplicate)."""
        store = _DedupStore(db_path=None)  # in-memory
        is_dup = store.record_or_skip("slack", "event-001")
        assert is_dup is False, "First call must return False (not a duplicate)"
        store.close()

    def test_record_or_skip_second_call_returns_true(self) -> None:
        """record_or_skip returns True on second call with same event_id (duplicate)."""
        store = _DedupStore(db_path=None)
        store.record_or_skip("slack", "event-002")
        is_dup = store.record_or_skip("slack", "event-002")
        assert is_dup is True, "Second call must return True (duplicate)"
        store.close()

    def test_record_or_skip_different_platforms_are_independent(self) -> None:
        """Same event_id on different platforms are treated independently."""
        store = _DedupStore(db_path=None)
        # Same event_id, different platforms
        is_dup_slack = store.record_or_skip("slack", "event-cross")
        is_dup_telegram = store.record_or_skip("telegram", "event-cross")
        assert is_dup_slack is False
        assert is_dup_telegram is False, "Same event_id on different platform is not a dup"
        store.close()

    def test_record_or_skip_is_synchronous(self) -> None:
        """record_or_skip() is a regular (non-async) method — must be callable without await."""
        store = _DedupStore(db_path=None)
        # If this were async, calling it without await would return a coroutine, not bool.
        result = store.record_or_skip("slack", "sync-check")
        assert isinstance(result, bool), f"Expected bool, got {type(result)}"
        store.close()

    def test_sweep_expired_removes_old_rows(self, tmp_path: Path) -> None:
        """sweep_expired() removes rows older than 24h TTL."""
        store = _DedupStore(db_path=tmp_path / "dedup.db")

        # Insert a row, then manually backdate it past the 24h TTL
        store.record_or_skip("slack", "old-event")
        cutoff = time.time() - 90000  # > 24h ago
        store._conn.execute(
            "UPDATE event_dedup SET seen_at = ? WHERE event_id = ?",
            (cutoff, "old-event"),
        )
        store._conn.commit()

        # Insert a fresh row
        store.record_or_skip("slack", "new-event")

        deleted = store.sweep_expired()
        assert deleted == 1, f"Expected 1 expired row deleted, got {deleted}"

        # Verify the fresh row is still present
        is_dup = store.record_or_skip("slack", "new-event")
        assert is_dup is True, "Fresh event must still be in store after sweep"
        store.close()

    def test_sweep_expired_returns_zero_when_nothing_expired(self) -> None:
        """sweep_expired() returns 0 when all rows are within the TTL window."""
        store = _DedupStore(db_path=None)
        store.record_or_skip("slack", "recent-event")

        deleted = store.sweep_expired()
        assert deleted == 0, f"Expected 0 deletions for a fresh row, got {deleted}"
        store.close()

    def test_sweep_expired_is_synchronous(self) -> None:
        """sweep_expired() is a regular (non-async) method."""
        store = _DedupStore(db_path=None)
        result = store.sweep_expired()
        assert isinstance(result, int), f"Expected int, got {type(result)}"
        store.close()


# ---------------------------------------------------------------------------
# _classify_prefix helper
# ---------------------------------------------------------------------------


class TestClassifyPrefix:
    def test_xoxb_classified(self) -> None:
        assert _classify_prefix("xoxb-something") == "xoxb"

    def test_xoxa_classified(self) -> None:
        assert _classify_prefix("xoxa-something") == "xoxa"

    def test_xoxp_classified(self) -> None:
        assert _classify_prefix("xoxp-something") == "xoxp"

    def test_xapp_classified(self) -> None:
        assert _classify_prefix("xapp-something") == "xapp"

    def test_empty_string_classified_as_empty(self) -> None:
        assert _classify_prefix("") == "empty"

    def test_unknown_prefix_classified_as_other(self) -> None:
        assert _classify_prefix("invalid-token") == "other"
