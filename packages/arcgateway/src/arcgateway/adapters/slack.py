"""SlackAdapter — Socket Mode platform adapter for arcgateway.

Ports the proven logic from arcagent.modules.slack.bot into the
arcgateway.adapters.BasePlatformAdapter Protocol. This adapter is the
canonical replacement; the legacy module is deprecated (see D-001 through D-025).

Design decisions preserved from SPEC-011 (D-001 … D-025):
  D-002  connect_async() / close_async() lifecycle
  D-003  @app.event("message") not @app.message() — catches all subtypes
  D-007  No thread replies; flat DM conversation (reply_to ignored for threads)
  D-013  Audit every message and auth rejection — no tokens in events
  D-015  Validate xoxb- and xapp- prefixes at construction (hard-fail, not warn)
  D-016  event.user must be in allowed_user_ids; empty = deny all (fail-closed)
  D-017  Check event.get("bot_id") to skip bot messages (NOT subtype check)
  D-018  Lazy import of slack-bolt; guard with ImportError
  D-023  Mock AsyncApp, WebClient, AsyncSocketModeHandler in tests

New in T1.7.2 (Hermes-pattern replay deduplication, T1.9):
  Slack Socket Mode can re-deliver events after a WebSocket reconnect.
  A SQLite dedup table keyed on (platform, event_id) with 24h TTL prevents
  on_message from being called twice for the same logical event.
  Background sweep runs every hour to prune stale rows.

Audit events emitted (SDD §4.2):
  gateway.adapter.connect / disconnect / fail
  gateway.message.received / deduped

Token handling (NIST 800-53 IA-5):
  Tokens arrive as constructor arguments, never written to disk or logs.
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

# Maximum message length (Slack safe limit — preserves D-011)
_MAX_MESSAGE_LENGTH = 4000

# Dedup table TTL: 24 hours in seconds
_DEDUP_TTL_SECONDS = 86400

# Dedup sweep interval: 1 hour
_DEDUP_SWEEP_INTERVAL_SECONDS = 3600


# ---------------------------------------------------------------------------
# Message splitting — copied from arcagent.modules.slack.bot (D-011 / D-024)
# Extract to shared utility at N=3; currently N=2.
# ---------------------------------------------------------------------------


def split_message(text: str, max_length: int = _MAX_MESSAGE_LENGTH) -> list[str]:
    """Split text into chunks respecting natural boundaries.

    Priority order:
    1. Double-newline (paragraph boundary)
    2. Single newline
    3. Hard split at max_length

    Args:
        text: The text to split.
        max_length: Maximum characters per chunk (Slack safe limit: 4000).

    Returns:
        List of text chunks, each <= max_length characters.
    """
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

        # Try paragraph boundary (double-newline)
        split_pos = chunk.rfind("\n\n")
        if split_pos > 0:
            chunks.append(remaining[:split_pos])
            remaining = remaining[split_pos + 2 :]
            continue

        # Try single newline
        split_pos = chunk.rfind("\n")
        if split_pos > 0:
            chunks.append(remaining[:split_pos])
            remaining = remaining[split_pos + 1 :]
            continue

        # Hard split — no natural boundary found
        chunks.append(remaining[:max_length])
        remaining = remaining[max_length:]

    return chunks


# ---------------------------------------------------------------------------
# Dedup store (Hermes pattern — T1.9)
# ---------------------------------------------------------------------------


class _DedupStore:
    """SQLite-backed event deduplication table with 24h TTL sweep.

    Schema:
        CREATE TABLE IF NOT EXISTS event_dedup (
            platform TEXT NOT NULL,
            event_id TEXT NOT NULL,
            seen_at REAL NOT NULL,
            PRIMARY KEY (platform, event_id)
        );
        CREATE INDEX IF NOT EXISTS ix_event_dedup_seen_at
            ON event_dedup(seen_at);

    Usage:
        is_duplicate = store.record_or_skip(platform, event_id)
        # True  → already seen (replay); skip
        # False → first time; process normally

    Thread safety: Uses a single connection in WAL mode. All calls happen on
    the asyncio event loop thread (no external thread access needed).
    """

    def __init__(self, db_path: Path | None = None) -> None:
        """Open (or create) the dedup database.

        Args:
            db_path: File path for the SQLite DB. None → in-memory DB (tests).
        """
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

    def record_or_skip(self, platform: str, event_id: str) -> bool:
        """Record the event; return True if it was already recorded (duplicate).

        Uses INSERT OR IGNORE so the first INSERT wins. If rows_affected == 0
        the row existed before → replay.

        Args:
            platform: Platform identifier (e.g. "slack").
            event_id: Platform-specific event envelope ID.

        Returns:
            True  if this event_id was already in the table (replay/duplicate).
            False if this is the first time we've seen this event_id.
        """
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO event_dedup (platform, event_id, seen_at) VALUES (?, ?, ?)",
            (platform, event_id, time.time()),
        )
        self._conn.commit()
        # rowcount == 0 means the INSERT was ignored → row pre-existed → duplicate
        return cur.rowcount == 0

    def sweep_expired(self) -> int:
        """Delete rows older than 24h. Returns the count of deleted rows."""
        cutoff = time.time() - _DEDUP_TTL_SECONDS
        cur = self._conn.execute(
            "DELETE FROM event_dedup WHERE seen_at < ?",
            (cutoff,),
        )
        self._conn.commit()
        return cur.rowcount

    def close(self) -> None:
        """Close the SQLite connection."""
        self._conn.close()


# ---------------------------------------------------------------------------
# SlackAdapter
# ---------------------------------------------------------------------------


class SlackAdapter:
    """Socket Mode Slack adapter implementing BasePlatformAdapter.

    Connects to Slack via Socket Mode WebSocket, routes authorised DMs to
    on_message, and delivers replies via chat_postMessage.

    Design choices (preserving SPEC-011):
    - No thread replies (D-007): reply_to is accepted but not used for threading.
    - Bot loop prevention (D-017): skips events where event.get("bot_id") is truthy.
    - Empty allowed_user_ids = deny all (D-016, fail-closed security posture).
    - Token prefix validation at construction time (D-015) — hard ValueError.
    - @app.event("message") catches all message subtypes (D-003).
    """

    name: str = "slack"

    def __init__(
        self,
        bot_token: str,
        app_token: str,
        allowed_user_ids: list[str],
        on_message: Callable[[InboundEvent], Awaitable[None]],
        dedup_db_path: Path | None = None,
    ) -> None:
        """Initialise the adapter and validate tokens eagerly.

        Args:
            bot_token: Slack bot OAuth token — must start with 'xoxb-'.
            app_token: Slack app-level token — must start with 'xapp-'.
            allowed_user_ids: Allowlisted Slack user IDs. Empty = deny all.
            on_message: Async callback invoked for each authorised message.
            dedup_db_path: Path to SQLite dedup DB. None = in-memory (tests).

        Raises:
            ValueError: If bot_token does not start with 'xoxb-'.
            ValueError: If app_token does not start with 'xapp-'.
        """
        # D-015: Validate token prefixes at construction — hard fail with clear error.
        # This catches misconfigured tokens before any network call is made.
        if not bot_token.startswith("xoxb-"):
            msg = (
                f"Invalid bot_token: expected prefix 'xoxb-', "
                f"got {bot_token[:10]!r}. Check your Slack bot OAuth token."
            )
            raise ValueError(msg)

        if not app_token.startswith("xapp-"):
            msg = (
                f"Invalid app_token: expected prefix 'xapp-', "
                f"got {app_token[:10]!r}. Check your Slack app-level token."
            )
            raise ValueError(msg)

        self._bot_token = bot_token
        self._app_token = app_token
        self._allowed_user_ids = list(allowed_user_ids)
        self._on_message = on_message

        # Dedup store (Hermes replay-dedup pattern — T1.9)
        self._dedup = _DedupStore(db_path=dedup_db_path)
        self._dedup_sweep_task: asyncio.Task[None] | None = None

        # slack-bolt objects populated in connect().
        # Typed as Any because slack-bolt is an optional dep; mypy cannot
        # verify the concrete types without the package installed.
        self._app: Any = None
        self._handler: Any = None

    # ── BasePlatformAdapter interface ───────────────────────────────────────

    async def connect(self) -> None:
        """Establish Socket Mode connection.

        Lazily imports slack-bolt so the package is optional (D-018).
        Starts the dedup TTL sweep background task.

        Raises:
            ImportError: If slack-bolt is not installed.
            RuntimeError: If Socket Mode connection fails.
        """
        # D-018: Lazy import guarded by ImportError.
        # The pyproject.toml mypy override (ignore_missing_imports = true) for
        # slack_bolt.* suppresses the missing-stub error at the import site,
        # so no type: ignore comment is needed on the imports themselves.
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

        # D-003: @app.event("message") catches all subtypes (not @app.message()).
        # self._app is Any (optional dep). mypy emits [untyped-decorator] because
        # it cannot resolve the return type of Any.__call__; suppressed below.
        @self._app.event("message")  # type: ignore[untyped-decorator]
        async def _handle_message(event: dict[str, Any]) -> None:
            await self._handle_inbound(event)

        # No-op handlers suppress slack-bolt WARNING logs for edit/delete events.
        @self._app.event("message_changed")  # type: ignore[untyped-decorator]
        async def _handle_changed(event: dict[str, Any]) -> None:
            pass

        @self._app.event("message_deleted")  # type: ignore[untyped-decorator]
        async def _handle_deleted(event: dict[str, Any]) -> None:
            pass

        self._handler = AsyncSocketModeHandler(self._app, self._app_token)
        try:
            # D-002: connect_async() is non-blocking (NOT start_async()).
            await self._handler.connect_async()
        except Exception as exc:
            _logger.exception("SlackAdapter: Socket Mode connection failed")
            raise RuntimeError("Slack Socket Mode connection failed") from exc

        # Start dedup TTL sweep — runs every hour to prune 24h-old rows.
        self._dedup_sweep_task = asyncio.create_task(
            self._dedup_sweep_loop(),
            name="arcgateway.slack.dedup_sweep",
        )

        _logger.info("SlackAdapter: connected via Socket Mode")
        # TODO (M1 telemetry): emit gateway.adapter.connect audit event

    async def disconnect(self) -> None:
        """Close Socket Mode connection and cancel background tasks."""
        # Cancel the dedup sweep task cleanly.
        if self._dedup_sweep_task is not None and not self._dedup_sweep_task.done():
            self._dedup_sweep_task.cancel()
            try:
                await self._dedup_sweep_task
            except asyncio.CancelledError:
                pass
            self._dedup_sweep_task = None

        if self._handler is not None:
            try:
                # D-002: close_async() for clean shutdown (NOT disconnect_async()).
                await self._handler.close_async()
            except Exception:
                _logger.exception("SlackAdapter: error closing Socket Mode handler")
            self._handler = None

        self._app = None
        self._dedup.close()
        _logger.info("SlackAdapter: disconnected")
        # TODO (M1 telemetry): emit gateway.adapter.disconnect audit event

    async def send(
        self,
        target: DeliveryTarget,
        message: str,
        *,
        reply_to: str | None = None,
    ) -> None:
        """Send a message to a Slack channel via chat_postMessage.

        D-007: No thread replies for flat DM conversations.
        reply_to is accepted (Protocol compliance) but not used for threading.

        Args:
            target: Delivery address; target.chat_id is the Slack channel ID.
            message: Text to deliver. Automatically split at 4000-char boundaries.
            reply_to: Accepted but unused (no thread replies per D-007).

        Raises:
            RuntimeError: If adapter is not connected.
        """
        if self._app is None:
            msg = "SlackAdapter.send() called before connect()"
            raise RuntimeError(msg)

        chunks = split_message(message, _MAX_MESSAGE_LENGTH)
        for chunk in chunks:
            await self._app.client.chat_postMessage(
                channel=target.chat_id,
                text=chunk,
            )

    # ── Internal message handling ───────────────────────────────────────────

    async def _handle_inbound(self, event: dict[str, Any]) -> None:
        """Route an inbound Slack message event.

        Guards (in order):
        1. D-017 — skip bot messages (event.get("bot_id") truthy)
        2. T1.9  — dedup check (replay protection via SQLite)
        3. D-016 — authorisation check (allowed_user_ids allowlist)
        4. Dispatch to on_message callback

        Args:
            event: Raw Slack event payload from Socket Mode.
        """
        # D-017: Skip bot messages to prevent infinite loops.
        # Use event.get("bot_id"), NOT subtype check (per decision D-017).
        if event.get("bot_id"):
            return

        user_id: str = event.get("user", "")
        channel: str = event.get("channel", "")
        text: str = event.get("text", "") or ""

        if not user_id:
            return

        # T1.9: Replay deduplication — Slack Socket Mode can redeliver events
        # after a WebSocket reconnect. The event_id is in the envelope, but
        # individual message events use "client_msg_id" or we fall back to
        # a composite key. The Socket Mode handler embeds the envelope id in
        # the event dict as "event_id" when available.
        event_id = event.get("client_msg_id") or event.get("event_id") or ""
        if event_id:
            is_replay = self._dedup.record_or_skip("slack", event_id)
            if is_replay:
                _logger.debug(
                    "SlackAdapter: dropping replay event_id=%r user=%r",
                    event_id,
                    user_id,
                )
                # Emit structured log in lieu of full telemetry wiring (M1).
                # TODO (M1 telemetry): emit gateway.message.deduped audit event
                _logger.info(
                    "gateway.message.deduped platform=slack event_id=%r user=%r",
                    event_id,
                    user_id,
                )
                return

        # D-016: Authorisation check — empty allowlist = deny all (fail-closed).
        if not self._is_authorised(user_id):
            _logger.warning(
                "SlackAdapter: unauthorised user %r rejected (allowed: %s)",
                user_id,
                self._allowed_user_ids or "[]",
            )
            # TODO (M1 telemetry): emit gateway.message.auth_rejected audit event
            return

        # Build normalised InboundEvent for the session router / on_message callback.
        inbound = InboundEvent(
            platform="slack",
            chat_id=channel,
            user_did=f"slack:{user_id}",   # identity graph resolves to DID in T1.3
            agent_did="",                   # populated by GatewayRunner in T1.4 wiring
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
        """Return True if user_id is in the allowlist.

        D-016: Empty allowlist = deny all (fail-closed).

        Args:
            user_id: Slack user ID to check.

        Returns:
            True if authorised, False otherwise.
        """
        return user_id in self._allowed_user_ids

    # ── Dedup TTL sweep ─────────────────────────────────────────────────────

    async def _dedup_sweep_loop(self) -> None:
        """Background task: sweep expired dedup rows every hour.

        Runs until cancelled (disconnect()). Each sweep removes rows older
        than 24h to bound SQLite storage growth.
        """
        while True:
            await asyncio.sleep(_DEDUP_SWEEP_INTERVAL_SECONDS)
            try:
                deleted = self._dedup.sweep_expired()
                if deleted:
                    _logger.debug("SlackAdapter: dedup sweep removed %d expired rows", deleted)
            except Exception:
                # Sweep failure is non-fatal; log and continue.
                _logger.exception("SlackAdapter: dedup sweep error (non-fatal)")
