"""SlackAdapter -- Socket Mode platform adapter for arcgateway.

Performance (SPEC-018 Wave B1):
  _DedupStore.record_or_skip and sweep_expired are synchronous SQLite
  calls.  Async wrappers offload them to asyncio.to_thread so _handle_inbound
  and _dedup_sweep_loop never block the event loop.
  Sync implementations kept as _sync_record_or_skip / _sync_sweep_expired.

Design decisions preserved from SPEC-011:
  D-002  connect_async() / close_async() lifecycle
  D-003  @app.event("message") catches all subtypes
  D-007  No thread replies
  D-013  Audit every message -- no tokens in events
  D-015  Validate xoxb- and xapp- prefixes at construction
  D-016  event.user must be in allowed_user_ids; empty = deny all
  D-017  Check event.get("bot_id") to skip bot messages
  D-018  Lazy import of slack-bolt
  D-023  Mock AsyncApp, WebClient, AsyncSocketModeHandler in tests

Hermes-pattern replay deduplication (T1.9):
  SQLite dedup table keyed on (platform, event_id) with 24h TTL.
  Background sweep runs every hour.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from arcgateway.delivery import DeliveryTarget
from arcgateway.executor import InboundEvent

_logger = logging.getLogger("arcgateway.adapters.slack")

_MAX_MESSAGE_LENGTH = 4000
_DEDUP_TTL_SECONDS = 86400
_DEDUP_SWEEP_INTERVAL_SECONDS = 3600


def split_message(text: str, max_length: int = _MAX_MESSAGE_LENGTH) -> list[str]:
    """Split text into chunks respecting natural boundaries."""
    if not text:
        return []
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break

        chunk = remaining[:max_length]

        split_pos = chunk.rfind("\n\n")
        if split_pos > 0:
            chunks.append(remaining[:split_pos])
            remaining = remaining[split_pos + 2:]
            continue

        split_pos = chunk.rfind("\n")
        if split_pos > 0:
            chunks.append(remaining[:split_pos])
            remaining = remaining[split_pos + 1:]
            continue

        chunks.append(remaining[:max_length])
        remaining = remaining[max_length:]

    return chunks


class _DedupStore:
    """SQLite-backed event deduplication with 24h TTL sweep.

    Async API (for event-loop callers):
        await store.record_or_skip(platform, event_id)
        await store.sweep_expired()

    Sync API (for tests):
        store._sync_record_or_skip(platform, event_id)
        store._sync_sweep_expired()
    """

    def __init__(self, db_path: "Path | None" = None) -> None:
        connect_str = str(db_path) if db_path is not None else ":memory:"
        self._conn = sqlite3.connect(connect_str, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS event_dedup (
                platform TEXT NOT NULL,
                event_id TEXT NOT NULL,
                seen_at  REAL NOT NULL,
                PRIMARY KEY (platform, event_id)
            )
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_event_dedup_seen_at
                ON event_dedup(seen_at)
            """
        )
        self._conn.commit()

    def _sync_record_or_skip(self, platform: str, event_id: str) -> bool:
        """Sync: Record event; return True if already recorded (duplicate)."""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO event_dedup (platform, event_id, seen_at) VALUES (?, ?, ?)",
            (platform, event_id, time.time()),
        )
        self._conn.commit()
        return cur.rowcount == 0

    def _sync_sweep_expired(self) -> int:
        """Sync: Delete rows older than 24h; return count deleted."""
        cutoff = time.time() - _DEDUP_TTL_SECONDS
        cur = self._conn.execute(
            "DELETE FROM event_dedup WHERE seen_at < ?",
            (cutoff,),
        )
        self._conn.commit()
        return cur.rowcount

    async def record_or_skip(self, platform: str, event_id: str) -> bool:
        """Async wrapper: offloads _sync_record_or_skip to thread pool."""
        return await asyncio.to_thread(self._sync_record_or_skip, platform, event_id)

    async def sweep_expired(self) -> int:
        """Async wrapper: offloads _sync_sweep_expired to thread pool."""
        return await asyncio.to_thread(self._sync_sweep_expired)

    def close(self) -> None:
        self._conn.close()


_TOKEN_PREFIX_CLASSES: tuple[str, ...] = ("xoxb", "xoxa", "xoxp", "xapp")


def _classify_prefix(token: str) -> str:
    if not token:
        return "empty"
    for cls in _TOKEN_PREFIX_CLASSES:
        if token.startswith(cls + "-"):
            return cls
    return "other"


class SlackAdapter:
    """Socket Mode Slack adapter implementing BasePlatformAdapter."""

    name: str = "slack"

    def __init__(
        self,
        bot_token: str,
        app_token: str,
        allowed_user_ids: list[str],
        on_message: "Callable[[InboundEvent], Awaitable[None]]",
        dedup_db_path: "Path | None" = None,
    ) -> None:
        if not bot_token.startswith("xoxb-"):
            msg = (
                f"bot_token must start with xoxb-; got "
                f"{len(bot_token)}-char string with prefix type "
                f"{_classify_prefix(bot_token)}. "
                "Check your Slack bot OAuth token."
            )
            raise ValueError(msg)

        if not app_token.startswith("xapp-"):
            msg = (
                f"app_token must start with xapp-; got "
                f"{len(app_token)}-char string with prefix type "
                f"{_classify_prefix(app_token)}. "
                "Check your Slack app-level token."
            )
            raise ValueError(msg)

        self._bot_token = bot_token
        self._app_token = app_token
        self._allowed_user_ids = list(allowed_user_ids)
        self._on_message = on_message

        self._dedup = _DedupStore(db_path=dedup_db_path)
        self._dedup_sweep_task: "asyncio.Task[None] | None" = None

        self._app: Any = None
        self._handler: Any = None

    async def connect(self) -> None:
        """Establish Socket Mode connection."""
        try:
            from slack_bolt.adapter.socket_mode.async_handler import (
                AsyncSocketModeHandler,
            )
            from slack_bolt.async_app import AsyncApp
        except ImportError as exc:
            msg = (
                "slack-bolt is not installed. "
                "Install with: pip install 'arcgateway[slack]'"
            )
            raise ImportError(msg) from exc

        self._app = AsyncApp(token=self._bot_token)

        @self._app.event("message")  # type: ignore[untyped-decorator]
        async def _handle_message(event: "dict[str, Any]") -> None:
            await self._handle_inbound(event)

        @self._app.event("message_changed")  # type: ignore[untyped-decorator]
        async def _handle_changed(event: "dict[str, Any]") -> None:
            pass

        @self._app.event("message_deleted")  # type: ignore[untyped-decorator]
        async def _handle_deleted(event: "dict[str, Any]") -> None:
            pass

        self._handler = AsyncSocketModeHandler(self._app, self._app_token)
        try:
            await self._handler.connect_async()
        except Exception as exc:
            _logger.exception("SlackAdapter: Socket Mode connection failed")
            raise RuntimeError("Slack Socket Mode connection failed") from exc

        self._dedup_sweep_task = asyncio.create_task(
            self._dedup_sweep_loop(),
            name="arcgateway.slack.dedup_sweep",
        )

        _logger.info("SlackAdapter: connected via Socket Mode")

    async def disconnect(self) -> None:
        """Close Socket Mode connection and cancel background tasks."""
        if self._dedup_sweep_task is not None and not self._dedup_sweep_task.done():
            self._dedup_sweep_task.cancel()
            try:
                await self._dedup_sweep_task
            except asyncio.CancelledError:
                pass
            self._dedup_sweep_task = None

        if self._handler is not None:
            try:
                await self._handler.close_async()
            except Exception:
                _logger.exception("SlackAdapter: error closing Socket Mode handler")
            self._handler = None

        self._app = None
        self._dedup.close()
        _logger.info("SlackAdapter: disconnected")

    async def send(
        self,
        target: DeliveryTarget,
        message: str,
        *,
        reply_to: "str | None" = None,
    ) -> None:
        if self._app is None:
            msg = "SlackAdapter.send() called before connect()"
            raise RuntimeError(msg)

        chunks = split_message(message, _MAX_MESSAGE_LENGTH)
        for chunk in chunks:
            await self._app.client.chat_postMessage(
                channel=target.chat_id,
                text=chunk,
            )

    async def send_with_id(self, target: DeliveryTarget, message: str) -> "str | None":
        if self._app is None:
            msg = "SlackAdapter.send_with_id() called before connect()"
            raise RuntimeError(msg)

        first_chunk = split_message(message, _MAX_MESSAGE_LENGTH)
        text = first_chunk[0] if first_chunk else message

        response = await self._app.client.chat_postMessage(
            channel=target.chat_id,
            text=text,
        )

        ts: "str | None" = None
        if response and isinstance(response, dict):
            ts = response.get("ts")
        elif response is not None and hasattr(response, "get"):
            ts = response.get("ts")

        _logger.debug(
            "SlackAdapter.send_with_id: delivered to channel=%s ts=%s",
            target.chat_id,
            ts,
        )
        return ts

    async def edit_message(
        self,
        target: DeliveryTarget,
        message_id: str,
        new_text: str,
    ) -> None:
        if self._app is None:
            msg = "SlackAdapter.edit_message() called before connect()"
            raise RuntimeError(msg)

        text = new_text[:_MAX_MESSAGE_LENGTH]

        try:
            await self._app.client.chat_update(
                channel=target.chat_id,
                ts=message_id,
                text=text,
            )
        except Exception as exc:
            _logger.warning(
                "SlackAdapter.edit_message: failed to edit ts=%r channel=%r: %s",
                message_id,
                target.chat_id,
                exc,
            )
            raise

        _logger.debug(
            "SlackAdapter.edit_message: updated ts=%s channel=%s",
            message_id,
            target.chat_id,
        )

    async def _handle_inbound(self, event: "dict[str, Any]") -> None:
        """Route an inbound Slack message event."""
        if event.get("bot_id"):
            return

        user_id: str = event.get("user", "")
        channel: str = event.get("channel", "")
        text: str = event.get("text", "") or ""

        if not user_id:
            return

        event_id = event.get("client_msg_id") or event.get("event_id") or ""
        if event_id:
            is_replay = await self._dedup.record_or_skip("slack", event_id)
            if is_replay:
                _logger.debug(
                    "SlackAdapter: dropping replay event_id=%r user=%r",
                    event_id,
                    user_id,
                )
                _logger.info(
                    "gateway.message.deduped platform=slack event_id=%r user=%r",
                    event_id,
                    user_id,
                )
                return

        if not self._is_authorised(user_id):
            _logger.warning(
                "SlackAdapter: unauthorised user %r rejected (allowed: %s)",
                user_id,
                self._allowed_user_ids or "[]",
            )
            return

        inbound = InboundEvent(
            platform="slack",
            chat_id=channel,
            user_did=f"slack:{user_id}",
            agent_did="",
            session_key=f"slack:{channel}:{user_id}",
            message=text,
            raw_payload=dict(event),
        )

        _logger.debug(
            "SlackAdapter: dispatching message user=%r channel=%r len=%d",
            user_id,
            channel,
            len(text),
        )
        await self._on_message(inbound)

    def _is_authorised(self, user_id: str) -> bool:
        return user_id in self._allowed_user_ids

    async def _dedup_sweep_loop(self) -> None:
        """Background task: sweep expired dedup rows every hour."""
        while True:
            await asyncio.sleep(_DEDUP_SWEEP_INTERVAL_SECONDS)
            try:
                deleted = await self._dedup.sweep_expired()
                if deleted:
                    _logger.debug("SlackAdapter: dedup sweep removed %d expired rows", deleted)
            except Exception:
                _logger.exception("SlackAdapter: dedup sweep error (non-fatal)")
