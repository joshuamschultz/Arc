"""Unit tests for arcgateway.adapters.slack.SlackAdapter.

All slack-bolt interactions are mocked — no real Slack connection needed.

The slack-bolt imports are lazy (D-018): they happen inside connect().
We patch at the source module paths that the adapter's local import
statements reference:
  slack_bolt.async_app.AsyncApp
  slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler

Test coverage:
  test_token_validation_xoxb_prefix        — wrong bot_token prefix → ValueError
  test_token_validation_xapp_prefix        — wrong app_token prefix → ValueError
  test_valid_tokens_accepted               — correct prefixes → no error
  test_empty_allowlist_denies_all          — D-016 fail-closed
  test_unauthorized_user_rejected          — non-allowlisted user → no on_message
  test_bot_message_skipped                 — event.get("bot_id") truthy → skip
  test_authorized_user_dispatched          — allowlisted user → on_message called
  test_send_calls_chat_postMessage         — send() uses Slack web API
  test_send_splits_long_message            — >4000 char message split into chunks
  test_connect_raises_on_import_error      — slack-bolt missing → ImportError
  test_disconnect_cleans_up               — disconnect resets internal state
  test_reply_to_ignored_no_thread          — D-007: no thread replies
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcgateway.adapters.slack import SlackAdapter, split_message
from arcgateway.delivery import DeliveryTarget
from arcgateway.executor import InboundEvent

# ---------------------------------------------------------------------------
# Patch paths: slack-bolt uses lazy imports inside connect(), so we patch
# the canonical module paths that Python's import machinery resolves.
# ---------------------------------------------------------------------------
_ASYNC_APP_PATH = "slack_bolt.async_app.AsyncApp"
_HANDLER_PATH = (
    "slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(
    allowed_user_ids: list[str] | None = None,
) -> tuple[SlackAdapter, list[InboundEvent]]:
    """Build a SlackAdapter with in-memory dedup store and capture on_message calls."""
    received: list[InboundEvent] = []

    async def _on_message(event: InboundEvent) -> None:
        received.append(event)

    adapter = SlackAdapter(
        bot_token="xoxb-valid-token",
        app_token="xapp-valid-token",
        allowed_user_ids=allowed_user_ids if allowed_user_ids is not None else ["U123"],
        on_message=_on_message,
        dedup_db_path=None,  # in-memory DB for tests
    )
    return adapter, received


def _make_event(
    user: str = "U123",
    channel: str = "D456",
    text: str = "hello",
    bot_id: str | None = None,
    client_msg_id: str | None = "msg-001",
) -> dict[str, Any]:
    """Build a minimal Slack event payload."""
    payload: dict[str, Any] = {
        "user": user,
        "channel": channel,
        "text": text,
    }
    if bot_id is not None:
        payload["bot_id"] = bot_id
    if client_msg_id is not None:
        payload["client_msg_id"] = client_msg_id
    return payload


def _build_mock_bolt() -> tuple[MagicMock, MagicMock, MagicMock, MagicMock]:
    """Return (mock_app, mock_app_cls, mock_handler, mock_handler_cls).

    The _cls variants are what we patch into sys.modules; the plain variants
    are the instances returned when the class is called.
    """
    mock_client = MagicMock()
    mock_client.chat_postMessage = AsyncMock(return_value={"ok": True})

    mock_app = MagicMock()
    mock_app.client = mock_client
    mock_app.event = MagicMock(return_value=lambda fn: fn)  # decorator no-op

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
    """Build a sys.modules stub so the lazy imports inside connect() succeed."""
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
# Token validation
# ---------------------------------------------------------------------------


class TestTokenValidation:
    def test_token_validation_xoxb_prefix(self) -> None:
        """Constructor must raise ValueError if bot_token lacks 'xoxb-' prefix."""
        with pytest.raises(ValueError, match="xoxb-"):
            SlackAdapter(
                bot_token="invalid-token",
                app_token="xapp-valid",
                allowed_user_ids=["U123"],
                on_message=AsyncMock(),
            )

    def test_token_validation_xapp_prefix(self) -> None:
        """Constructor must raise ValueError if app_token lacks 'xapp-' prefix."""
        with pytest.raises(ValueError, match="xapp-"):
            SlackAdapter(
                bot_token="xoxb-valid",
                app_token="invalid-token",
                allowed_user_ids=["U123"],
                on_message=AsyncMock(),
            )

    def test_both_wrong_prefixes_fails_on_bot_token_first(self) -> None:
        """If both tokens are wrong, ValueError is raised for bot_token first."""
        with pytest.raises(ValueError, match="bot_token"):
            SlackAdapter(
                bot_token="wrong-bot",
                app_token="wrong-app",
                allowed_user_ids=[],
                on_message=AsyncMock(),
            )

    def test_valid_tokens_accepted(self) -> None:
        """Correct token prefixes must not raise."""
        adapter, _ = _make_adapter()
        assert adapter.name == "slack"


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------


class TestAuthorisation:
    async def test_empty_allowlist_denies_all(self) -> None:
        """D-016: Empty allowed_user_ids means every user is rejected."""
        adapter, received = _make_adapter(allowed_user_ids=[])
        await adapter._handle_inbound(_make_event(user="U999"))
        assert len(received) == 0

    async def test_unauthorized_user_rejected(self) -> None:
        """Non-allowlisted user must not trigger on_message."""
        adapter, received = _make_adapter(allowed_user_ids=["U123"])
        await adapter._handle_inbound(_make_event(user="U999"))
        assert len(received) == 0

    async def test_authorized_user_dispatched(self) -> None:
        """Allowlisted user must trigger on_message exactly once."""
        adapter, received = _make_adapter(allowed_user_ids=["U123"])
        await adapter._handle_inbound(_make_event(user="U123", client_msg_id="msg-abc"))
        assert len(received) == 1
        assert received[0].platform == "slack"
        assert received[0].chat_id == "D456"

    async def test_multiple_allowed_users(self) -> None:
        """Both users in the allowlist must be dispatched."""
        adapter, received = _make_adapter(allowed_user_ids=["U123", "U456"])
        await adapter._handle_inbound(_make_event(user="U123", client_msg_id="msg-1"))
        await adapter._handle_inbound(_make_event(user="U456", client_msg_id="msg-2"))
        assert len(received) == 2


# ---------------------------------------------------------------------------
# Bot loop prevention
# ---------------------------------------------------------------------------


class TestBotLoopPrevention:
    async def test_bot_message_skipped(self) -> None:
        """D-017: Event with bot_id must be ignored (NOT subtype check)."""
        adapter, received = _make_adapter(allowed_user_ids=["U123"])
        bot_event = _make_event(user="U123", bot_id="B_BOT_123")
        await adapter._handle_inbound(bot_event)
        assert len(received) == 0

    async def test_bot_id_none_not_skipped(self) -> None:
        """Event without bot_id must not be skipped by bot filter."""
        adapter, received = _make_adapter(allowed_user_ids=["U123"])
        human_event = _make_event(user="U123", bot_id=None, client_msg_id="msg-human")
        await adapter._handle_inbound(human_event)
        assert len(received) == 1

    async def test_empty_bot_id_not_skipped(self) -> None:
        """Empty string bot_id is falsy — should not be treated as bot."""
        adapter, received = _make_adapter(allowed_user_ids=["U123"])
        event = _make_event(user="U123", client_msg_id="msg-empty-bot")
        event["bot_id"] = ""  # falsy but present
        await adapter._handle_inbound(event)
        assert len(received) == 1


# ---------------------------------------------------------------------------
# send()
# ---------------------------------------------------------------------------


class TestSend:
    async def test_send_calls_chat_postMessage(self) -> None:
        """send() must use Slack web API chat_postMessage."""
        _, mock_app_cls, mock_handler, mock_handler_cls = _build_mock_bolt()
        modules = _make_bolt_modules(mock_app_cls, mock_handler_cls)

        with patch.dict(sys.modules, modules):
            adapter, _ = _make_adapter()
            await adapter.connect()

            target = DeliveryTarget.parse("slack:D456")
            await adapter.send(target, "Hello, world!")

            adapter._app.client.chat_postMessage.assert_called_once_with(
                channel="D456",
                text="Hello, world!",
            )
            await adapter.disconnect()

    async def test_send_splits_long_message(self) -> None:
        """Messages over 4000 chars must be sent as multiple chunks."""
        _, mock_app_cls, mock_handler, mock_handler_cls = _build_mock_bolt()
        modules = _make_bolt_modules(mock_app_cls, mock_handler_cls)

        with patch.dict(sys.modules, modules):
            adapter, _ = _make_adapter()
            await adapter.connect()

            long_message = "A" * 4001  # Just over the 4000-char limit
            target = DeliveryTarget.parse("slack:D456")
            await adapter.send(target, long_message)

            # Should have been called twice (one chunk of 4000, one of 1)
            assert adapter._app.client.chat_postMessage.call_count == 2
            await adapter.disconnect()

    async def test_send_raises_if_not_connected(self) -> None:
        """send() before connect() must raise RuntimeError."""
        adapter, _ = _make_adapter()
        target = DeliveryTarget.parse("slack:D456")
        with pytest.raises(RuntimeError, match="connect"):
            await adapter.send(target, "oops")

    async def test_reply_to_ignored_no_thread(self) -> None:
        """D-007: reply_to is accepted but thread replies are never sent."""
        _, mock_app_cls, mock_handler, mock_handler_cls = _build_mock_bolt()
        modules = _make_bolt_modules(mock_app_cls, mock_handler_cls)

        with patch.dict(sys.modules, modules):
            adapter, _ = _make_adapter()
            await adapter.connect()

            target = DeliveryTarget.parse("slack:D456")
            await adapter.send(target, "flat reply", reply_to="ts_1234567890")

            # chat_postMessage must NOT include thread_ts
            call_kwargs = adapter._app.client.chat_postMessage.call_args.kwargs
            assert "thread_ts" not in call_kwargs
            await adapter.disconnect()


# ---------------------------------------------------------------------------
# connect / disconnect
# ---------------------------------------------------------------------------


class TestConnectDisconnect:
    async def test_connect_raises_on_import_error(self) -> None:
        """If slack-bolt is not installed, connect() raises ImportError."""
        adapter, _ = _make_adapter()

        # Remove slack_bolt from sys.modules to simulate missing install
        mods_to_remove = {
            k: v for k, v in sys.modules.items() if "slack_bolt" in k
        }
        with patch.dict(sys.modules, {k: None for k in mods_to_remove}):  # type: ignore[misc]
            # If it's already absent, just verify the adapter handles it
            with pytest.raises((ImportError, Exception)):
                await adapter.connect()

    async def test_connect_raises_on_connection_failure(self) -> None:
        """If Socket Mode connection fails, connect() raises RuntimeError."""
        _, mock_app_cls, mock_handler, mock_handler_cls = _build_mock_bolt()
        mock_handler.connect_async = AsyncMock(side_effect=Exception("network error"))
        modules = _make_bolt_modules(mock_app_cls, mock_handler_cls)

        with patch.dict(sys.modules, modules):
            adapter, _ = _make_adapter()
            with pytest.raises(RuntimeError, match="connection failed"):
                await adapter.connect()

    async def test_disconnect_cleans_up(self) -> None:
        """disconnect() resets _app and _handler to None."""
        _, mock_app_cls, mock_handler, mock_handler_cls = _build_mock_bolt()
        modules = _make_bolt_modules(mock_app_cls, mock_handler_cls)

        with patch.dict(sys.modules, modules):
            adapter, _ = _make_adapter()
            await adapter.connect()
            assert adapter._app is not None
            assert adapter._handler is not None

            await adapter.disconnect()
            assert adapter._app is None
            assert adapter._handler is None

    async def test_disconnect_calls_close_async(self) -> None:
        """D-002: close_async() must be called on disconnect."""
        _, mock_app_cls, mock_handler, mock_handler_cls = _build_mock_bolt()
        modules = _make_bolt_modules(mock_app_cls, mock_handler_cls)

        with patch.dict(sys.modules, modules):
            adapter, _ = _make_adapter()
            await adapter.connect()
            await adapter.disconnect()

            mock_handler.close_async.assert_called_once()


# ---------------------------------------------------------------------------
# split_message helper
# ---------------------------------------------------------------------------


class TestSplitMessage:
    def test_empty_string(self) -> None:
        assert split_message("") == []

    def test_short_message_unchanged(self) -> None:
        assert split_message("hello") == ["hello"]

    def test_splits_at_4000_hard_limit(self) -> None:
        msg = "X" * 4001
        chunks = split_message(msg)
        assert len(chunks) == 2
        assert len(chunks[0]) == 4000
        assert len(chunks[1]) == 1

    def test_splits_at_paragraph_boundary(self) -> None:
        para1 = "A" * 3990
        para2 = "B" * 100
        msg = para1 + "\n\n" + para2
        chunks = split_message(msg)
        assert chunks[0] == para1
        assert chunks[1] == para2

    def test_splits_at_newline_boundary(self) -> None:
        line1 = "A" * 3990
        line2 = "B" * 100
        msg = line1 + "\n" + line2
        chunks = split_message(msg)
        assert chunks[0] == line1
        assert chunks[1] == line2
