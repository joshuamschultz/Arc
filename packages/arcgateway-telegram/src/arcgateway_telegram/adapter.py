"""TelegramAdapter — platform adapter for Telegram Bot API long-polling.

Ports the proven polling/reconnect/auth logic from arcagent.modules.telegram.bot
into the BasePlatformAdapter Protocol consumed by GatewayRunner.

Design (SDD §3.1, PLAN T1.7.1 + T1.10):

Polling-conflict pattern (T1.10):
    Exactly one process can long-poll a given bot token. If a second gateway
    process is already polling, python-telegram-bot raises an exception whose
    message contains "terminated by other getUpdates request". A running
    polling loop cannot resolve this in place — a fresh process is the only
    remedy — so we log a LOUD warning (silent failure here is the #1
    production bug), back off briefly, and call _set_fatal_error(retryable=True)
    so GatewayRunner's reconnect watcher restarts us cleanly.

NetworkError:
    During initialization, transient network failures (no internet, DNS blip)
    are retried up to 5 times with exponential backoff capped at 60 s. In the
    polling loop, a NetworkError backs off once then sets a fatal-retryable
    error so GatewayRunner restarts the adapter.

Auth:
    Inbound user_id must be in allowed_user_ids. Empty allowlist = deny all
    (fail-closed, matching arcagent.modules.telegram.TelegramBot._is_authorized).
    Rejected messages emit a gateway.adapter.auth_rejected audit event and are
    silently ignored — no reply leaks information to an unauthorised sender.

Message splitting:
    Telegram limits messages to 4096 characters. split_message() is ported
    directly from arcagent.modules.telegram.bot to keep the proven behaviour.

python-telegram-bot is a dependency of the arcgateway-telegram package:
    Installing this extension package pulls in python-telegram-bot. The import
    is still guarded with try/except inside connect() so a clear error is
    raised if the runtime environment is somehow missing it. mypy suppresses
    missing-import errors for telegram.* via this package's pyproject overrides.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from arcgateway.adapters._backoff import exponential_backoff
from arcgateway.delivery import DeliveryTarget
from arcgateway.executor import InboundEvent

_logger = logging.getLogger("arcgateway_telegram.adapter")

# Sentence-ending punctuation for boundary detection (from arcagent.modules.telegram.bot)
_SENTENCE_END = re.compile(r"[.!?]\s")

# Telegram API hard limit for sendMessage
_TELEGRAM_MAX_MESSAGE_LENGTH = 4096

# ── Polling-conflict backoff before hand-off to GatewayRunner ────────────────
_CONFLICT_BACKOFF_SECONDS = 1.0

# ── NetworkError retry parameters ────────────────────────────────────────────
_NETWORK_MAX_RETRIES = 5
_NETWORK_BACKOFF_BASE_SECONDS = 2.0
_NETWORK_BACKOFF_CAP_SECONDS = 60.0

# ── Audit event names (SDD §4.2) ─────────────────────────────────────────────
_EVENT_CONNECT = "gateway.adapter.connect"
_EVENT_DISCONNECT = "gateway.adapter.disconnect"
_EVENT_FAIL = "gateway.adapter.fail"
_EVENT_AUTH_REJECTED = "gateway.adapter.auth_rejected"
_EVENT_MSG_RECEIVED = "gateway.message.received"
_EVENT_MSG_SENT = "gateway.message.sent"


def _last_sentence_boundary(window: str) -> int | None:
    """Return the index just past the last ``.!?`` in ``window`` (or None).

    The returned index keeps the punctuation in the left chunk; the shared
    splitter ``lstrip``-s the trailing whitespace from the remainder.
    """
    last: re.Match[str] | None = None
    for m in _SENTENCE_END.finditer(window):
        last = m
    if last is None:
        return None
    return last.end() - 1


def split_message(text: str, max_length: int = _TELEGRAM_MAX_MESSAGE_LENGTH) -> list[str]:
    """Split text into chunks respecting natural boundaries.

    Priority order:
    1. Double-newline (paragraph boundary)
    2. Single newline
    3. Sentence boundary (. ! ?)
    4. Hard split at max_length

    Delegates to the shared ``arcgateway.adapters._text.split_message`` (the
    one canonical splitter) with Telegram's sentence-ending fallback and 4096
    limit, so paragraph → newline → sentence → hard-cut behaviour is preserved.

    Args:
        text: The text to split.
        max_length: Maximum characters per chunk (Telegram limit: 4096).

    Returns:
        List of text chunks, each <= max_length characters.
    """
    from arcgateway.adapters._text import split_message as _shared_split

    return _shared_split(
        text,
        max_length,
        boundaries=("\n\n", "\n"),
        final_boundary=_last_sentence_boundary,
    )


class TelegramAdapter:
    """Platform adapter for Telegram Bot API long-polling.

    Implements the BasePlatformAdapter Protocol (SDD §3.1). Runs its own
    polling loop as a background asyncio.Task; crashes in this adapter
    are isolated by GatewayRunner's asyncio.TaskGroup (ASI08).

    Attributes:
        name: Adapter identifier, always "telegram".
        _bot_token: Bot API token. Never logged or stored to disk.
        _allowed_user_ids: Allowlist of authorised Telegram user IDs.
            Empty = deny all (fail-closed).
        _on_message: Callback wired by GatewayRunner to SessionRouter.handle().
        _application: python-telegram-bot Application instance (Any to avoid
            optional import at type-check time).
        _polling_task: Background asyncio.Task running the polling loop.
        _fatal_error: Set when a non-retryable or escalated error occurs.
        _fatal_retryable: Whether GatewayRunner should attempt to restart.
    """

    name = "telegram"

    def __init__(
        self,
        bot_token: str,
        allowed_user_ids: list[int],
        on_message: Callable[[InboundEvent], Awaitable[None]],
        *,
        agent_did: str = "did:arc:agent:default",
        poll_interval: float = 0.5,
    ) -> None:
        """Initialise TelegramAdapter.

        Args:
            bot_token: Telegram Bot API token. Never logged or persisted.
            allowed_user_ids: Allowlist of authorised Telegram user IDs.
                An empty list means deny all (fail-closed).
            on_message: Async callback receiving normalised InboundEvents.
                Typically SessionRouter.handle, wired by GatewayRunner.
            agent_did: DID of the ArcAgent this adapter serves.
            poll_interval: Seconds between Telegram long-poll requests.
        """
        if not bot_token:
            msg = "bot_token must not be empty"
            raise ValueError(msg)

        self._bot_token = bot_token
        self._allowed_user_ids = list(allowed_user_ids)
        self._on_message = on_message
        self._agent_did = agent_did
        self._poll_interval = poll_interval

        # Type is Any because python-telegram-bot is an optional dep and we
        # cannot reference its concrete type at import time without installing it.
        self._application: Any = None
        self._polling_task: asyncio.Task[None] | None = None
        self._bot_id: int | None = None
        self._running = False

        # Fatal error tracking — set by _set_fatal_error(), read by runner
        self._fatal_error: Exception | None = None
        self._fatal_retryable = False

    # ── BasePlatformAdapter Protocol ──────────────────────────────────────────

    async def connect(self) -> None:
        """Initialize the Telegram bot and start polling.

        Handles polling-conflict (bounded retries → fatal-retryable) and
        NetworkError (bounded retries → fatal-retryable). Returns promptly
        after starting the background polling task.

        Raises:
            ImportError: If python-telegram-bot is not installed.
            RuntimeError: On fatal auth failure (invalid token etc.).
        """
        try:
            from telegram.ext import Application
        except ImportError as exc:
            msg = (
                "python-telegram-bot is not installed. "
                "Install with: pip install 'arcgateway-telegram'"
            )
            raise ImportError(msg) from exc

        _logger.info("TelegramAdapter: connecting (agent_did=%s)", self._agent_did)
        self._running = True

        # Build the Application — this does NOT open a network connection yet.
        self._application = Application.builder().token(self._bot_token).build()
        self._register_handlers()

        # Initialize the bot (one-time API call to verify token + get bot_info).
        await self._initialize_with_retry()

        # Start polling in a background task so connect() returns promptly.
        self._polling_task = asyncio.create_task(
            self._run_polling_loop(),
            name="telegram:polling_loop",
        )

        _logger.info(
            "TelegramAdapter: connected (bot_id=%s agent=%s)",
            self._bot_id,
            self._agent_did,
        )
        self._audit(_EVENT_CONNECT, {"agent_did": self._agent_did})

    async def disconnect(self) -> None:
        """Stop polling and shut down the Telegram application cleanly.

        Must NOT raise — log errors and return. Called by GatewayRunner
        on shutdown and before reconnect attempts.
        """
        _logger.info("TelegramAdapter: disconnecting")
        self._running = False

        if self._polling_task is not None and not self._polling_task.done():
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
            except Exception:  # reason: fail-open — log + continue
                _logger.exception("TelegramAdapter: error awaiting polling task cancellation")
            self._polling_task = None

        if self._application is not None:
            try:
                updater = getattr(self._application, "updater", None)
                if updater is not None and getattr(updater, "running", False):
                    await updater.stop()
                await self._application.stop()
                await self._application.shutdown()
            except Exception:  # reason: fail-open — log + continue
                _logger.exception("TelegramAdapter: error shutting down application")
            self._application = None

        self._audit(_EVENT_DISCONNECT, {"agent_did": self._agent_did})
        _logger.info("TelegramAdapter: disconnected")

    async def send(
        self,
        target: DeliveryTarget,
        message: str,
        *,
        reply_to: str | None = None,
    ) -> None:
        """Send a message to a Telegram chat.

        Splits the message at natural boundaries (paragraph > newline >
        sentence > hard cut) to stay within Telegram's 4096-character limit.

        Args:
            target: DeliveryTarget with chat_id and optional thread_id.
            message: Text to deliver.
            reply_to: Optional message ID to reply to (Telegram message_id).

        Raises:
            RuntimeError: If the application is not connected.
        """
        if self._application is None:
            msg = "TelegramAdapter.send: not connected"
            raise RuntimeError(msg)

        chat_id_str = target.chat_id
        try:
            chat_id: int | str = int(chat_id_str)
        except ValueError:
            chat_id = chat_id_str

        reply_to_id: int | None = None
        if reply_to is not None:
            try:
                reply_to_id = int(reply_to)
            except ValueError:
                _logger.warning(
                    "TelegramAdapter.send: invalid reply_to %r — ignoring",
                    reply_to,
                )

        chunks = split_message(message)
        for chunk in chunks:
            await self._application.bot.send_message(
                chat_id=chat_id,
                text=chunk,
                reply_to_message_id=reply_to_id,
            )

        self._audit(
            _EVENT_MSG_SENT,
            {
                "chat_id": str(chat_id),
                "chunks": len(chunks),
                "agent_did": self._agent_did,
            },
        )
        _logger.debug(
            "TelegramAdapter.send: delivered %d chunk(s) to chat_id=%s",
            len(chunks),
            chat_id,
        )

    async def send_with_id(
        self,
        target: DeliveryTarget,
        message: str,
    ) -> str | None:
        """Send a single message and return the Telegram message_id as a string.

        Overrides the Protocol default to return a real message ID so
        StreamBridge can use it for edit/delete operations.

        Args:
            target: DeliveryTarget with chat_id.
            message: Text to send (single message, no chunk-splitting).

        Returns:
            str: Telegram message_id cast to str.
            None: On unexpected send failure (should not normally occur).

        Raises:
            RuntimeError: If not connected.
        """
        if self._application is None:
            msg = "TelegramAdapter.send_with_id: not connected"
            raise RuntimeError(msg)

        chat_id_str = target.chat_id
        try:
            chat_id: int | str = int(chat_id_str)
        except ValueError:
            chat_id = chat_id_str

        sent = await self._application.bot.send_message(
            chat_id=chat_id,
            text=message,
        )
        return str(sent.message_id)

    async def edit_message(
        self,
        target: DeliveryTarget,
        message_id: str,
        new_text: str,
    ) -> None:
        """Edit a previously-sent message — the streaming-update primitive.

        StreamBridge calls this to progressively replace the placeholder with
        accumulated tokens, so the reply "types out" in place like Slack.
        Telegram caps edit text at 4096 chars; longer turns are finalized by
        ``send()`` which splits at natural boundaries.

        Raises:
            RuntimeError: If the application is not connected.
        """
        if self._application is None:
            msg = "TelegramAdapter.edit_message: not connected"
            raise RuntimeError(msg)
        await self._application.bot.edit_message_text(
            chat_id=self._resolve_chat_id(target.chat_id),
            message_id=int(message_id),
            text=new_text[:_TELEGRAM_MAX_MESSAGE_LENGTH],
        )

    async def send_typing(self, target: DeliveryTarget) -> None:
        """Show the "typing…" indicator in the chat. Cosmetic; never raises hard.

        Telegram clears the indicator automatically after ~5s or when the next
        message arrives, so a single call at turn start is enough for the
        common case.
        """
        if self._application is None:
            return
        from telegram.constants import ChatAction

        await self._application.bot.send_chat_action(
            chat_id=self._resolve_chat_id(target.chat_id),
            action=ChatAction.TYPING,
        )

    @staticmethod
    def split_message(text: str) -> list[str]:
        """Split ``text`` into Telegram-sized chunks at natural boundaries.

        StreamBridge calls this to finalize a long streamed reply: the first
        chunk replaces the placeholder via edit_message and the rest are sent as
        follow-up messages, so replies over 4096 chars split instead of truncate.
        """
        return split_message(text)

    @staticmethod
    def _resolve_chat_id(chat_id_str: str) -> int | str:
        """Telegram chat ids are numeric; fall back to the raw string (@channel)."""
        try:
            return int(chat_id_str)
        except ValueError:
            return chat_id_str

    # ── Internal: Bot Setup ───────────────────────────────────────────────────

    def _register_handlers(self) -> None:
        """Register message handlers on the Application.

        Text messages and commands both route through _handle_update so the
        single handler covers all inbound text traffic.
        """
        # _register_handlers is only called from connect() after _application is set.
        if self._application is None:  # pragma: no cover
            msg = "_register_handlers called before connect()"
            raise RuntimeError(msg)

        from telegram.ext import (
            MessageHandler,
            filters,
        )

        # All text (including commands) routes to one handler.
        self._application.add_handler(MessageHandler(filters.TEXT, self._handle_update))
        # Error handler so update errors are logged rather than silently swallowed.
        self._application.add_error_handler(self._on_error)

    async def _initialize_with_retry(self) -> None:
        """Initialize the bot with NetworkError retry.

        Verifies the token and fetches bot_info. Raises on persistent failure.
        Called from connect() after _application is built.
        """
        # _initialize_with_retry is only called from connect() after _application is set.
        if self._application is None:  # pragma: no cover
            msg = "_initialize_with_retry called before connect()"
            raise RuntimeError(msg)

        max_retries = _NETWORK_MAX_RETRIES
        for attempt in range(1, max_retries + 1):
            try:
                await self._application.initialize()
                bot_info = await self._application.bot.get_me()
                self._bot_id = bot_info.id
                _logger.info(
                    "TelegramAdapter: authenticated as @%s (id=%d)",
                    bot_info.username,
                    bot_info.id,
                )
                return
            except Exception as exc:  # reason: fail-open — log + continue
                if _is_network_error(exc):
                    backoff = _network_backoff(attempt)
                    _logger.warning(
                        "TelegramAdapter: NetworkError during init (attempt %d/%d): %s. "
                        "Retrying in %.0fs.",
                        attempt,
                        max_retries,
                        exc,
                        backoff,
                    )
                    if attempt < max_retries:
                        await asyncio.sleep(backoff)
                        continue
                    # Exhausted retries
                    self._set_fatal_error(exc, retryable=True)
                    raise RuntimeError(
                        f"TelegramAdapter: persistent NetworkError during init after "
                        f"{max_retries} attempts"
                    ) from exc
                # Non-network error (bad token, auth failure) — fatal, not retryable
                self._set_fatal_error(exc, retryable=False)
                raise

    # ── Internal: Polling Loop ────────────────────────────────────────────────

    async def _run_polling_loop(self) -> None:
        """Run the Telegram polling loop, handing failures to GatewayRunner.

        This task lives for the lifetime of the adapter. It:
        1. Starts the application and updater.
        2. On a polling-conflict or NetworkError, backs off once then sets
           _set_fatal_error(retryable=True) so GatewayRunner's reconnect
           watcher restarts the adapter cleanly (a fresh process is the only
           thing that resolves a getUpdates conflict — looping in-place cannot).
        3. On any other exception, sets _set_fatal_error(retryable=False) and
           re-raises for human attention.
        """
        try:
            from telegram import Update
        except ImportError:
            _logger.error("TelegramAdapter: python-telegram-bot not available in polling loop")
            return

        # _run_polling_loop is launched from connect() after _application is built.
        if self._application is None:  # pragma: no cover
            msg = "_run_polling_loop called before connect()"
            raise RuntimeError(msg)

        try:
            await self._application.start()
            await self._application.updater.start_polling(
                poll_interval=self._poll_interval,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
            )
            _logger.info("TelegramAdapter: polling started")

            # Polling is now running in the background via PTB's updater.
            # We keep this task alive until _running is False so GatewayRunner
            # can cancel it cleanly on shutdown.
            while self._running:
                await asyncio.sleep(1.0)

        except asyncio.CancelledError:
            # Clean shutdown — let it propagate.
            raise

        except Exception as exc:  # reason: fail-open — log + continue
            if _is_conflict_error(exc):
                _logger.warning(
                    "TelegramAdapter: POLLING CONFLICT detected. Another gateway "
                    "process may be polling this bot token. Only one gateway "
                    "instance may poll a given bot token — use NATS routing for "
                    "multi-instance. Handing off to GatewayRunner in %.0fs. Error: %s",
                    _CONFLICT_BACKOFF_SECONDS,
                    exc,
                )
                await asyncio.sleep(_CONFLICT_BACKOFF_SECONDS)
                self._set_fatal_error(exc, retryable=True)
                return

            if _is_network_error(exc):
                backoff = _network_backoff(1)
                _logger.warning(
                    "TelegramAdapter: NetworkError in polling loop: %s. "
                    "Handing off to GatewayRunner in %.0fs.",
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                self._set_fatal_error(exc, retryable=True)
                return

            _logger.exception("TelegramAdapter: unhandled error in polling loop: %s", exc)
            self._set_fatal_error(exc, retryable=False)
            raise

    # ── Internal: Message Handling ────────────────────────────────────────────

    async def _handle_update(self, update: Any, context: Any) -> None:
        """Process one inbound Telegram message update.

        Performs auth check first; rejected messages are silently ignored
        after emitting an audit event (no reply to avoid info leakage).
        Authorised messages are wrapped in InboundEvent and forwarded to
        the on_message callback (SessionRouter.handle in production).

        Args:
            update: python-telegram-bot Update object.
            context: python-telegram-bot CallbackContext (unused).
        """
        if update.effective_message is None or update.effective_user is None:
            return

        user_id: int = update.effective_user.id
        chat_id = str(update.effective_chat.id) if update.effective_chat else str(user_id)

        # Skip our own bot messages to prevent self-talk loops.
        if self._bot_id is not None and user_id == self._bot_id:
            return

        # Auth check — empty allowlist = deny all (fail-closed).
        if not self._is_authorized(user_id):
            _logger.warning(
                "TelegramAdapter: auth rejected for user_id=%d (allowed_user_ids count=%d)",
                user_id,
                len(self._allowed_user_ids),
            )
            self._audit(
                _EVENT_AUTH_REJECTED,
                {
                    "platform": "telegram",
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "agent_did": self._agent_did,
                },
            )
            # Silent ignore — no reply (avoids confirming bot existence to attacker)
            return

        text: str | None = update.effective_message.text
        if not text:
            return

        # Build normalised user DID from Telegram user_id.
        # Full cross-platform identity graph resolution is T1.3;
        # for now we derive a stable platform-scoped DID.
        user_did = f"did:arc:telegram:{user_id}"

        event = InboundEvent(
            platform="telegram",
            chat_id=chat_id,
            thread_id=None,
            user_did=user_did,
            agent_did=self._agent_did,
            session_key=f"{self._agent_did}:telegram:private:{user_id}",
            message=text,
            raw_payload={
                "update_id": update.update_id,
                "user_id": user_id,
                "chat_id": chat_id,
            },
        )

        self._audit(
            _EVENT_MSG_RECEIVED,
            {
                "platform": "telegram",
                "user_did": user_did,
                "chat_id": chat_id,
                "agent_did": self._agent_did,
            },
        )

        try:
            await self._on_message(event)
        except Exception:  # reason: fail-open — log + continue
            _logger.exception(
                "TelegramAdapter: error in on_message callback for user_did=%s",
                user_did,
            )

    async def _on_error(self, update: Any, context: Any) -> None:
        """Log errors from python-telegram-bot update processing."""
        _logger.error(
            "TelegramAdapter: update error: %s (update=%s)",
            context.error,
            update,
            exc_info=context.error,
        )

    # ── Internal: Auth ────────────────────────────────────────────────────────

    def _is_authorized(self, user_id: int) -> bool:
        """Check if user_id is in the allowlist.

        Empty allowlist = deny all (fail-closed). Matches the behaviour of
        arcagent.modules.telegram.TelegramBot._is_authorized.

        Args:
            user_id: Telegram user ID.

        Returns:
            True if user_id is in allowed_user_ids, False otherwise.
        """
        return user_id in self._allowed_user_ids

    # ── Internal: Error Management ────────────────────────────────────────────

    def _set_fatal_error(self, exc: Exception, *, retryable: bool) -> None:
        """Record a fatal error and mark whether the runner should restart.

        Called before returning from the polling loop to signal GatewayRunner.

        Args:
            exc: The exception that triggered the fatal condition.
            retryable: True = runner should restart the adapter (e.g. conflict,
                transient network); False = manual intervention required.
        """
        self._fatal_error = exc
        self._fatal_retryable = retryable
        self._audit(
            _EVENT_FAIL,
            {
                "platform": "telegram",
                "error": str(exc),
                "retryable": retryable,
                "agent_did": self._agent_did,
            },
        )

    # ── Internal: Audit ───────────────────────────────────────────────────────

    def _audit(self, event_name: str, data: dict[str, Any]) -> None:
        """Emit a structured audit log entry.

        Routes through both structured stdlib logging (log-aggregator
        compatibility) and ``arcgateway.audit.emit_event`` (canonical
        arctrust.audit sink pipeline for tamper-evidence).
        """
        _logger.info(
            "AUDIT event=%s data=%s",
            event_name,
            data,
            extra={"audit_event": event_name, "audit_data": data},
        )
        # Canonical arctrust.audit sink — swallows errors per AU-5.
        from arcgateway.audit import emit_event as _arc_emit

        _outcome = "deny" if "rejected" in event_name or "fail" in event_name else "allow"
        _arc_emit(
            action=event_name,
            target=data.get("chat_id") or data.get("agent_did") or "telegram",
            outcome=_outcome,
            extra=data,
        )


# ── Error classification helpers ──────────────────────────────────────────────


def _is_conflict_error(exc: Exception) -> bool:
    """Return True if the exception represents a Telegram polling conflict.

    python-telegram-bot raises a Conflict exception (subclass of TelegramError)
    when another process is already polling the same bot token. We check both
    the exception type name and the message to be defensive against library
    version differences.

    Args:
        exc: Exception to classify.

    Returns:
        True if this is a polling-conflict error.
    """
    # Check exception class hierarchy names (avoids importing the library)
    type_name = type(exc).__name__
    if "Conflict" in type_name:
        return True

    # Fallback: check error message (python-telegram-bot v20 Conflict exception)
    msg = str(exc).lower()
    return "terminated by other getupdates" in msg or "conflict" in msg


def _is_network_error(exc: Exception) -> bool:
    """Return True if the exception is a transient network failure.

    Args:
        exc: Exception to classify.

    Returns:
        True if this is a network error worth retrying.
    """
    type_name = type(exc).__name__
    return "NetworkError" in type_name or "TimedOut" in type_name


def _network_backoff(attempt: int) -> float:
    """Compute exponential backoff for network errors, capped at 60 s.

    Formula: min(2**(attempt-1) * base, cap)
    attempt=1 → 2s, 2 → 4s, 3 → 8s, 4 → 16s, 5+ → 60s

    Args:
        attempt: 1-indexed retry attempt number.

    Returns:
        Seconds to sleep before the next attempt.
    """
    return exponential_backoff(
        attempt,
        base=_NETWORK_BACKOFF_BASE_SECONDS,
        factor=2.0,
        cap=_NETWORK_BACKOFF_CAP_SECONDS,
    )
