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
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from arcgateway.adapters._media import (
    describe_undeliverable,
    kind_for,
    read_artefact,
    read_bounded,
)
from arcgateway.adapters._text import split_for_platform
from arcgateway.adapters.base import (
    DraftPart,
    InboundDraft,
    Outbound,
    PendingMedia,
)
from arcgateway.adapters.base import as_parts as _as_parts
from arcgateway.audit import emit_event
from arcgateway.commands.base import CommandSpec
from arcgateway.delivery import DeliveryTarget
from arcgateway.parts import MediaPart, TextPart

_logger = logging.getLogger("arcgateway.adapters.slack.adapter")

_MAX_MESSAGE_LENGTH = 4000
_DEDUP_TTL_SECONDS = 86400
_DEDUP_SWEEP_INTERVAL_SECONDS = 3600

#: Hosts Slack serves private files from. A workspace's own ``*.slack.com``
#: subdomain also issues file links, so the suffix match covers both.
_SLACK_FILE_HOSTS: tuple[str, ...] = ("slack.com", "slack-edge.com", "slack-files.com")

#: Bound on any single outbound HTTP call, so a stalled remote cannot hold a
#: session task for aiohttp's five-minute default.
_HTTP_TIMEOUT_SECONDS = 30


class _DedupStore:
    """SQLite-backed event deduplication with 24h TTL sweep.

    Sync API (synchronous SQLite calls):
        store.record_or_skip(platform, event_id)
        store.sweep_expired()
    """

    def __init__(self, db_path: Path | None = None) -> None:
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

    def record_or_skip(self, platform: str, event_id: str) -> bool:
        """Check and record an event for deduplication.

        Returns False on first sight (not a duplicate). Returns True if this
        platform+event_id combination was already seen (duplicate → skip it).

        Synchronous because SQLite operations are CPU-bound and short. Callers
        in async contexts may use ``asyncio.to_thread(store.record_or_skip, ...)``
        if they need to avoid blocking the event loop.
        """
        return self._sync_record_or_skip(platform, event_id)

    def sweep_expired(self) -> int:
        """Remove expired dedup entries and return count of rows deleted.

        Synchronous wrapper around the SQLite sweep operation.
        """
        return self._sync_sweep_expired()

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


def _assert_slack_host(url: str) -> None:
    """Refuse to send the bot token anywhere Slack does not serve files from.

    The download URL arrives inside the event payload, which is remote input,
    and the fetch attaches ``Authorization: Bearer <bot_token>``. Without this
    check a single crafted ``url_private_download`` field walks the workspace
    token to a host of the sender's choosing (SEC-26 / LLM02).

    Matched on the parsed host with an exact-or-dot-suffix test, never a
    substring: ``slack.com.attacker.example`` contains "slack.com" and is not
    Slack, and ``files.slack.com.evil.test`` is the same trick one label along.
    """
    host = (urlparse(url).hostname or "").lower()
    if not any(host == domain or host.endswith("." + domain) for domain in _SLACK_FILE_HOSTS):
        msg = f"{host or url!r} is not a Slack file host; refusing to send the bot token"
        raise ValueError(msg)


class SlackAdapter:
    """Socket Mode Slack adapter implementing BasePlatformAdapter."""

    name: str = "slack"

    max_message_chars = _MAX_MESSAGE_LENGTH
    """Declared, not enforced here — the gateway splits (REQ-310)."""

    supports: tuple[str, ...] = ("text", "image", "file", "edit", "message_id")
    """Capabilities this adapter really has, and the gate ``_post_media`` obeys.

    One source with the descriptor, so a kind the contract suite believes is
    unsupported cannot be one ``send`` quietly uploads anyway.
    """

    text_boundaries: tuple[str, ...] = ("\n\n", "\n")
    """Slack's UI wraps long lines gracefully, so paragraph → newline is enough."""

    def __init__(
        self,
        bot_token: str,
        app_token: str,
        allowed_user_ids: list[str],
        on_message: Callable[[InboundDraft], Awaitable[None]],
        *,
        agent_did: str = "did:arc:agent:default",
        dedup_db_path: Path | None = None,
        require_pairing: bool = False,
    ) -> None:
        """Initialise SlackAdapter.

        Args:
            require_pairing: Mirrors ``[security].require_pairing``. Auth is
                "static allowed_user_ids OR approved-paired": when True, a
                user who fails the static check is still forwarded to
                on_message so SessionRouter's PairingInterceptor can mint/DM
                a pairing code or route an already-approved user through,
                instead of being silently dropped.
        """
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
        self._agent_did = agent_did
        self.agent_did = agent_did
        self._require_pairing = require_pairing

        self._dedup = _DedupStore(db_path=dedup_db_path)
        self._dedup_sweep_task: asyncio.Task[None] | None = None

        self._app: Any = None
        self._handler: Any = None
        self._command_names: tuple[str, ...] = ()
        # One session per adapter lifetime for file downloads; created lazily so
        # a personal-tier test that never networks can still construct.
        self._http_session: Any | None = None

    def set_command_names(self, specs: Sequence[CommandSpec]) -> None:
        """Register slash-command names to subscribe to on ``connect``.

        Slack — unlike Telegram — does NOT deliver slash commands as ``message``
        events, so each command must be explicitly subscribed via
        ``@app.command``. Bootstrap calls this with the gateway's
        ``CommandRegistry.command_specs()`` before ``connect``; only each spec's
        name is subscribed. Every name here must also be declared in the Slack
        app manifest with the ``commands`` scope (a manifest/reinstall step —
        code alone cannot register Slack commands).
        """
        self._command_names = tuple(spec.name for spec in specs)

    async def connect(self) -> None:
        """Establish Socket Mode connection."""
        try:
            # slack-bolt ships no type stubs — the slack_bolt.* mypy override
            # (ignore_missing_imports) keeps --strict green; the lazy import
            # path keeps the dependency optional at runtime.
            from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
            from slack_bolt.async_app import AsyncApp
        except ImportError as exc:
            msg = "slack-bolt is not installed. Install with: pip install 'arcgateway[slack]'"
            raise ImportError(msg) from exc

        self._app = AsyncApp(token=self._bot_token)

        @self._app.event("message")  # type: ignore[untyped-decorator]  # reason: slack_bolt's @app.event decorator is untyped; mypy treats it as decorating away the function signature
        async def _handle_message(event: dict[str, Any]) -> None:
            await self._handle_inbound(event)

        @self._app.event("message_changed")  # type: ignore[untyped-decorator]  # reason: slack_bolt's @app.event decorator is untyped; mypy treats it as decorating away the function signature
        async def _handle_changed(event: dict[str, Any]) -> None:
            pass

        @self._app.event("message_deleted")  # type: ignore[untyped-decorator]  # reason: slack_bolt's @app.event decorator is untyped; mypy treats it as decorating away the function signature
        async def _handle_deleted(event: dict[str, Any]) -> None:
            pass

        # Slash commands arrive as a separate `command` payload, not a message
        # event — subscribe each so it reaches the gateway's one command
        # interceptor as an InboundEvent whose message is "/<name> <text>".
        for _name in self._command_names:

            @self._app.command(f"/{_name}")  # type: ignore[untyped-decorator]  # reason: slack_bolt's @app.command decorator is untyped
            async def _handle_command(ack: Any, command: dict[str, Any]) -> None:
                await ack()  # Slack requires an ack within 3s
                await self._handle_slash_command(command)

        self._handler = AsyncSocketModeHandler(self._app, self._app_token)
        try:
            await self._handler.connect_async()
        except Exception as exc:  # reason: re-raise after log
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
            except Exception:  # reason: fail-open — log + continue
                _logger.exception("SlackAdapter: error closing Socket Mode handler")
            self._handler = None

        if self._http_session is not None and not self._http_session.closed:
            try:
                await self._http_session.close()
            except Exception:  # reason: fail-open — log + continue
                _logger.debug("SlackAdapter: http session close failed")
        self._http_session = None

        self._app = None
        self._dedup.close()
        _logger.info("SlackAdapter: disconnected")

    async def send(
        self,
        target: DeliveryTarget,
        parts: Outbound,
        *,
        reply_to: str | None = None,
    ) -> None:
        """Deliver a reply — words, artefacts, or both (REQ-311).

        An artefact Slack cannot upload becomes a text description naming the
        file, so a failed attachment costs the attachment and not the turn.
        """
        if self._app is None:
            msg = "SlackAdapter.send() called before connect()"
            raise RuntimeError(msg)

        for part in _as_parts(parts):
            if isinstance(part, TextPart):
                await self._post_text(target.chat_id, part.text)
            else:
                await self._post_media(target.chat_id, part)

    async def _post_text(self, channel: str, text: str) -> None:
        """Put words on the channel, split by the gateway at Slack's limit."""
        for chunk in split_for_platform(self, text):
            await self._app.client.chat_postMessage(channel=channel, text=chunk)

    async def _post_media(self, channel: str, part: MediaPart) -> None:
        """Upload one artefact, or say what it was and where it is."""
        if part.kind not in self.supports:
            await self._post_text(channel, describe_undeliverable(part, "Slack"))
            return
        try:
            payload = read_artefact(part)
            await self._app.client.files_upload_v2(
                channel=channel,
                filename=part.declared_name,
                content=payload,
            )
        except Exception as exc:  # reason: the turn outlives a refused upload
            _logger.warning(
                "SlackAdapter.send: %s %r not delivered (%s) — describing it instead",
                part.kind,
                part.declared_name,
                exc,
            )
            await self._post_text(channel, describe_undeliverable(part, "Slack"))
            return

        emit_event(
            action="media.sent",
            target=part.ref,
            outcome="allow",
            actor_did=self._agent_did,
            extra={
                "channel": f"slack:{channel}",
                "kind": part.kind,
                "mime": part.mime,
                "size_bytes": len(payload),
            },
        )

    async def send_with_id(self, target: DeliveryTarget, message: str) -> str | None:
        if self._app is None:
            msg = "SlackAdapter.send_with_id() called before connect()"
            raise RuntimeError(msg)

        first_chunk = split_for_platform(self, message)
        text = first_chunk[0] if first_chunk else message

        response = await self._app.client.chat_postMessage(
            channel=target.chat_id,
            text=text,
        )

        ts: str | None = None
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
        except Exception as exc:  # reason: re-raise after log
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

    async def _handle_inbound(self, event: dict[str, Any]) -> None:
        """Route an inbound Slack message event."""
        if event.get("bot_id"):
            return

        user_id: str = event.get("user", "")
        channel: str = event.get("channel", "")

        if not user_id:
            return

        event_id = event.get("client_msg_id") or event.get("event_id") or ""
        if event_id:
            is_replay = self._dedup.record_or_skip("slack", event_id)
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
                # Canonical arctrust.audit emit for replay deduplication.
                from arcgateway.audit import emit_event as _arc_emit

                _arc_emit(
                    action="gateway.message.deduped",
                    target=f"slack:{channel}",
                    outcome="deny",
                    extra={"platform": "slack", "event_id": event_id},
                )
                return

        # Auth: static allowed_user_ids OR approved-paired. Static check first
        # (empty allowlist = deny all, fail-closed). On failure:
        # require_pairing=False preserves the original silent-drop behaviour;
        # require_pairing=True forwards to on_message instead — SessionRouter's
        # PairingInterceptor makes the final call (mint+DM a pairing code, or
        # route through if `arc gateway pair approve` has already approved
        # this user in the gateway's pairing records).
        if not self._is_authorised(user_id):
            if not self._require_pairing:
                _logger.warning(
                    "SlackAdapter: unauthorised user %r rejected (allowed: %s)",
                    user_id,
                    self._allowed_user_ids or "[]",
                )
                from arcgateway.audit import emit_event as _arc_emit

                _arc_emit(
                    action="gateway.adapter.auth_rejected",
                    target=f"slack:{channel}",
                    outcome="deny",
                    extra={"platform": "slack", "agent_did": self._agent_did},
                )
                return
            _logger.info(
                "SlackAdapter: user %r not in static allowlist — forwarding for "
                "pairing check (require_pairing=true)",
                user_id,
            )

        parts = self.to_parts(event)
        if not parts:
            return

        inbound = InboundDraft(
            platform="slack",
            chat_id=channel,
            user_did=f"slack:{user_id}",
            agent_did=self._agent_did,
            parts=parts,
            raw_payload=dict(event),
        )

        _logger.debug(
            "SlackAdapter: dispatching message user=%r channel=%r parts=%d",
            user_id,
            channel,
            len(parts),
        )
        await self._on_message(inbound)

    def to_parts(self, payload: Any) -> list[DraftPart]:
        """Turn one Slack message event into ordered parts (REQ-296).

        A Slack file share is a ``message`` event carrying ``files`` — often
        with no text at all, which is exactly the case a text-only reader drops
        on the floor.

        Args:
            payload: The Slack ``message`` event dict.

        Returns:
            The comment (if any) followed by one part per shared file.
        """
        parts: list[DraftPart] = []
        text = str(payload.get("text", "") or "")
        if text:
            parts.append(TextPart(text=text))
        for shared in payload.get("files", []) or []:
            if isinstance(shared, dict):
                parts.append(self._pending(shared))
        return parts

    def _pending(self, shared: dict[str, Any]) -> PendingMedia:
        """Describe one shared file, with a closure that fetches it on demand.

        Slack's ``url_private_download`` needs the bot token, so the fetch has
        to happen here where the token lives — but only the *fetch* does. The
        gateway still decides whether the bytes are small enough to keep and
        where they land.
        """
        mime = str(shared.get("mimetype") or "application/octet-stream")
        declared_name = str(shared.get("name") or "file")
        url = str(shared.get("url_private_download") or shared.get("url_private") or "")
        file_id = str(shared.get("id") or "")
        size = shared.get("size")

        async def fetch(limit_bytes: int) -> bytes:
            return await self._download(url, file_id, limit_bytes=limit_bytes)

        return PendingMedia(
            kind=kind_for(mime),
            mime=mime,
            declared_name=declared_name,
            fetch=fetch,
            size_bytes=size if isinstance(size, int) else None,
        )

    async def _download(self, url: str, file_id: str, *, limit_bytes: int) -> bytes:
        """Read a shared file's bytes off Slack, bounded, resolving the URL if needed.

        Raises:
            ValueError: The URL does not belong to Slack, so the bot token is
                not sent to it.
            MediaTooLargeOnWireError: The body passed ``limit_bytes``.
        """
        if not url and file_id:
            info = await self._app.client.files_info(file=file_id)
            url = str(info.get("file", {}).get("url_private_download", ""))
        if not url:
            msg = f"Slack file {file_id!r} has no download URL"
            raise RuntimeError(msg)
        _assert_slack_host(url)

        session = await self._ensure_http_session()
        async with session.get(
            url, headers={"Authorization": f"Bearer {self._bot_token}"}
        ) as response:
            response.raise_for_status()
            return await read_bounded(response, limit_bytes)

    async def _ensure_http_session(self) -> Any:
        """Lazily create the per-adapter aiohttp session used for downloads.

        Carries an explicit timeout: aiohttp's default is five minutes, long
        enough for one stalled remote to hold a session task and its in-flight
        slot well past any useful bound.
        """
        if self._http_session is None or self._http_session.closed:
            import aiohttp

            self._http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SECONDS)
            )
        return self._http_session

    async def _handle_slash_command(self, command: dict[str, Any]) -> None:
        """Re-inject a Slack slash command as a normal InboundEvent.

        Slack delivers slash commands out-of-band from messages, so we
        reconstruct the ``/<name> <text>`` line and forward it exactly like a
        message. The gateway's single command interceptor then handles it —
        identical to Telegram/web, which deliver ``/<name>`` as message text.
        """
        name = str(command.get("command", "")).lstrip("/")
        text = str(command.get("text", "")).strip()
        user_id = str(command.get("user_id", ""))
        channel = str(command.get("channel_id", ""))
        message = f"/{name} {text}".strip()

        inbound = InboundDraft(
            platform="slack",
            chat_id=channel,
            user_did=f"slack:{user_id}",
            agent_did=self._agent_did,
            parts=[TextPart(text=message)],
            raw_payload=dict(command),
        )
        await self._on_message(inbound)

    def _is_authorised(self, user_id: str) -> bool:
        return user_id in self._allowed_user_ids

    async def _dedup_sweep_loop(self) -> None:
        """Background task: sweep expired dedup rows every hour."""
        while True:
            await asyncio.sleep(_DEDUP_SWEEP_INTERVAL_SECONDS)
            try:
                deleted = self._dedup.sweep_expired()
                if deleted:
                    _logger.debug("SlackAdapter: dedup sweep removed %d expired rows", deleted)
            except Exception:  # reason: fail-open — log + continue
                _logger.exception("SlackAdapter: dedup sweep error (non-fatal)")
