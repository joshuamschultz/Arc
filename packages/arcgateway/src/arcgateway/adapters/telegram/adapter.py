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
    Telegram limits messages to 4096 characters. The *gateway* splits (REQ-310);
    this adapter only declares ``max_message_chars`` and its boundaries.

Media (SPEC-065 REQ-296/311):
    Every inbound kind reaches the gateway, not text alone — the original defect
    was ``MessageHandler(filters.TEXT, …)``, under which a photo never reached a
    handler and no run ever started. Artefacts are handed up as ``PendingMedia``
    the gateway fetches and stores; this adapter never writes a file, names one,
    or applies a size ceiling.

python-telegram-bot is an optional dependency (``arcgateway[telegram]``):
    The import is guarded with try/except inside connect() so a deployment
    without it skips the platform with a clear reason instead of crashing.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Iterator, Sequence
from typing import Any

from arcgateway.adapters._backoff import exponential_backoff
from arcgateway.adapters._media import (
    describe_undeliverable,
    kind_for,
    read_artefact,
)
from arcgateway.adapters._text import split_for_platform
from arcgateway.adapters.base import (
    DraftPart,
    InboundDraft,
    MediaKind,
    MediaTooLargeOnWireError,
    Outbound,
    PendingMedia,
)
from arcgateway.adapters.base import as_parts as _as_parts
from arcgateway.audit import emit_event
from arcgateway.commands.base import CommandSpec
from arcgateway.delivery import DeliveryTarget
from arcgateway.parts import MediaPart, TextPart

_logger = logging.getLogger("arcgateway.adapters.telegram.adapter")

# Telegram API hard limit for sendMessage
_TELEGRAM_MAX_MESSAGE_LENGTH = 4096

# setMyCommands constraints: a command is 1-32 chars of [a-z0-9_]; a description
# is at most 256 chars; and the menu holds at most 100 commands.
_TELEGRAM_COMMAND_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_TELEGRAM_MAX_COMMAND_DESCRIPTION = 256
_TELEGRAM_MAX_COMMANDS = 100

# ── Polling-conflict backoff before hand-off to GatewayRunner ────────────────
_CONFLICT_BACKOFF_SECONDS = 1.0

# ── NetworkError retry parameters ────────────────────────────────────────────
_NETWORK_MAX_RETRIES = 5
_NETWORK_BACKOFF_BASE_SECONDS = 2.0
_NETWORK_BACKOFF_CAP_SECONDS = 60.0

# Consecutive getUpdates errors (which PTB surfaces to the error handler while
# keeping the poll loop alive) before escalating to a fatal-retryable reconnect.
_MAX_CONSECUTIVE_UPDATE_ERRORS = 5

# ── Audit event names (SDD §4.2) ─────────────────────────────────────────────
_EVENT_CONNECT = "gateway.adapter.connect"
_EVENT_DISCONNECT = "gateway.adapter.disconnect"
_EVENT_FAIL = "gateway.adapter.fail"
_EVENT_AUTH_REJECTED = "gateway.adapter.auth_rejected"
_EVENT_MSG_RECEIVED = "gateway.message.received"
_EVENT_MSG_SENT = "gateway.message.sent"


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
        _closed_event: Set when the event-source loop dies; awaited by
            wait_closed() so GatewayRunner observes a silent polling death.
        _consecutive_update_errors: Streak of getUpdates errors; escalates to a
            fatal-retryable reconnect once it reaches _MAX_CONSECUTIVE_UPDATE_ERRORS.
        _drop_pending_updates: True only for the first clean start; a reconnect
            must NOT drop the messages that queued during the outage.
    """

    name = "telegram"

    max_message_chars = _TELEGRAM_MAX_MESSAGE_LENGTH
    """Declared, not enforced here — the gateway splits (REQ-310)."""

    supports: tuple[str, ...] = (
        "text",
        "image",
        "file",
        "audio",
        "edit",
        "typing",
        "message_id",
    )
    """Capabilities this adapter really has, and the gate ``send`` gates on.

    Declared on the adapter rather than beside the descriptor so the set the
    contract suite reads and the set ``_send_media`` obeys cannot drift apart:
    a kind added to one and forgotten in the other is a silent degrade the
    tests would confirm as correct.
    """

    text_boundaries: tuple[str, ...] = ("\n\n", "\n")
    """Where a Telegram reader expects a break, in priority order."""

    def __init__(
        self,
        bot_token: str,
        allowed_user_ids: list[int],
        on_message: Callable[[InboundDraft], Awaitable[None]],
        *,
        agent_did: str = "did:arc:agent:default",
        poll_interval: float = 0.5,
        require_pairing: bool = False,
    ) -> None:
        """Initialise TelegramAdapter.

        Args:
            bot_token: Telegram Bot API token. Never logged or persisted.
            allowed_user_ids: Allowlist of authorised Telegram user IDs.
                An empty list means deny all (fail-closed) UNLESS
                require_pairing is True, in which case unlisted users fall
                through to the DM pairing flow instead of being dropped.
            on_message: Async callback receiving normalised InboundEvents.
                Typically SessionRouter.handle, wired by GatewayRunner.
            agent_did: DID of the ArcAgent this adapter serves.
            poll_interval: Seconds between Telegram long-poll requests.
            require_pairing: Mirrors ``[security].require_pairing``. Auth is
                "static allowed_user_ids OR approved-paired": when True, a
                user who fails the static check is still forwarded to
                on_message so SessionRouter's PairingInterceptor can mint/DM
                a pairing code or route an already-approved user through,
                instead of being silently dropped.
        """
        if not bot_token:
            msg = "bot_token must not be empty"
            raise ValueError(msg)

        self._bot_token = bot_token
        self._allowed_user_ids = list(allowed_user_ids)
        self._on_message = on_message
        self._agent_did = agent_did
        # Public: the SessionRouter keys its outbound registry by (name,
        # agent_did) so a reply returns through THIS bot, not another agent's
        # bot on the same platform.
        self.agent_did = agent_did
        self._poll_interval = poll_interval
        self._require_pairing = require_pairing

        # Type is Any because python-telegram-bot is an optional dep and we
        # cannot reference its concrete type at import time without installing it.
        self._application: Any = None
        self._polling_task: asyncio.Task[None] | None = None
        self._bot_id: int | None = None
        self._running = False
        # The native command menu published via setMyCommands on connect().
        self._command_specs: tuple[CommandSpec, ...] = ()

        # Fatal error tracking — set by _set_fatal_error(), observed by the
        # runner through wait_closed() (which unblocks on _closed_event).
        self._fatal_error: Exception | None = None
        self._fatal_retryable = False
        self._closed_event = asyncio.Event()
        self._consecutive_update_errors = 0
        self._drop_pending_updates = True

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
                "Install with: pip install 'arcgateway[telegram]'"
            )
            raise ImportError(msg) from exc

        _logger.info("TelegramAdapter: connecting (agent_did=%s)", self._agent_did)
        self._running = True

        # Re-arm liveness so a reconnect starts observably clean: a fresh
        # _closed_event the runner's wait_closed() can block on, and cleared
        # fatal/error state. _drop_pending_updates is intentionally NOT reset —
        # it stays False after the first start so a reconnect keeps queued msgs.
        self._fatal_error = None
        self._fatal_retryable = False
        self._consecutive_update_errors = 0
        self._closed_event = asyncio.Event()

        # Build the Application — this does NOT open a network connection yet.
        # concurrent_updates(True) lets PTB dispatch updates concurrently so a
        # single DM user's later messages are not serialized behind an in-flight
        # turn's inline pre-dispatch awaits (a wedged turn must not freeze the DM).
        self._application = (
            Application.builder().token(self._bot_token).concurrent_updates(True).build()
        )
        self._register_handlers()

        # Initialize the bot (one-time API call to verify token + get bot_info).
        await self._initialize_with_retry()

        # Publish the native "/" command menu now that the bot is live. Cosmetic
        # and best-effort: a failed setMyCommands must not stop the adapter.
        await self._publish_command_menu()

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

    def set_command_names(self, specs: Sequence[CommandSpec]) -> None:
        """Record the slash-command menu to publish to Telegram on ``connect``.

        Telegram delivers slash commands as ordinary ``message`` text (unlike
        Slack), so nothing here is needed for dispatch — this only drives the
        native "/" menu the client shows. The specs are stored and published via
        ``setMyCommands`` once the bot is live; illegal names are filtered there.
        """
        self._command_specs = tuple(specs)

    async def _publish_command_menu(self) -> None:
        """Publish the recorded specs as Telegram's native command menu.

        Names Telegram will not accept (uppercase, punctuation, over 32 chars)
        are skipped rather than rejecting the whole menu; descriptions are
        truncated to Telegram's 256-char limit and the list is capped at 100.
        Best-effort: a failed API call is logged, never raised.
        """
        if not self._command_specs or self._application is None:
            return
        from telegram import BotCommand

        commands: list[Any] = []
        for spec in self._command_specs:
            name = spec.name.lower()
            if not _TELEGRAM_COMMAND_RE.match(name):
                _logger.debug("TelegramAdapter: skipping illegal command name %r", spec.name)
                continue
            commands.append(
                BotCommand(
                    command=name,
                    description=spec.description[:_TELEGRAM_MAX_COMMAND_DESCRIPTION],
                )
            )
            if len(commands) >= _TELEGRAM_MAX_COMMANDS:
                break
        if not commands:
            return
        try:
            await self._application.bot.set_my_commands(commands)
        except Exception:  # reason: the "/" menu is cosmetic — never fail connect() for it
            _logger.warning("TelegramAdapter: set_my_commands failed", exc_info=True)

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
        parts: Outbound,
        *,
        reply_to: str | None = None,
    ) -> None:
        """Deliver a reply — words, artefacts, or both (REQ-311).

        Each part is delivered in the order the agent composed it. An artefact
        Telegram cannot carry, or refuses mid-upload, becomes a text
        description naming the file: the turn survives either way, because an
        operator who asked for a report and received silence has been failed
        whether or not an exception escaped.

        Args:
            target: DeliveryTarget with chat_id and optional thread_id.
            parts: The reply as parts, or plain text for the streaming path.
            reply_to: Optional message ID to reply to (Telegram message_id).

        Raises:
            RuntimeError: If the application is not connected.
        """
        if self._application is None:
            msg = "TelegramAdapter.send: not connected"
            raise RuntimeError(msg)

        chat_id = self._resolve_chat_id(target.chat_id)
        reply_to_id: int | None = None
        if reply_to is not None:
            try:
                reply_to_id = int(reply_to)
            except ValueError:
                _logger.warning(
                    "TelegramAdapter.send: invalid reply_to %r — ignoring",
                    reply_to,
                )

        chunks = 0
        for part in _as_parts(parts):
            if isinstance(part, TextPart):
                chunks += await self._send_text(chat_id, part.text, reply_to_id)
            else:
                chunks += await self._send_media(chat_id, part, reply_to_id)

        self._audit(
            _EVENT_MSG_SENT,
            {
                "chat_id": str(chat_id),
                "chunks": chunks,
                "agent_did": self._agent_did,
            },
        )

    async def _send_text(self, chat_id: int | str, text: str, reply_to_id: int | None) -> int:
        """Put words on the chat, split by the gateway at Telegram's limit."""
        chunks = split_for_platform(self, text)
        for chunk in chunks:
            await self._application.bot.send_message(
                chat_id=chat_id,
                text=chunk,
                reply_to_message_id=reply_to_id,
            )
        return len(chunks)

    async def _send_media(
        self, chat_id: int | str, part: MediaPart, reply_to_id: int | None
    ) -> int:
        """Put one artefact on the chat, or say what it was and where it is.

        Two ways Telegram can be unable to carry an artefact, and both end the
        same way. *Declared*: the kind is outside what this platform carries,
        which is known before trying. *Discovered*: the wire refuses the upload
        (Telegram answers an oversized document with a Bad Request), which
        nothing can know in advance.
        """
        if part.kind in self.supports:
            try:
                payload = read_artefact(part)
                await self._upload(chat_id, part, payload, reply_to_id)
            except Exception as exc:  # reason: the turn outlives a refused upload
                _logger.warning(
                    "TelegramAdapter.send: %s %r not delivered (%s) — describing it instead",
                    part.kind,
                    part.declared_name,
                    exc,
                )
            else:
                emit_event(
                    action="media.sent",
                    target=part.ref,
                    outcome="allow",
                    actor_did=self._agent_did,
                    extra={
                        "channel": f"telegram:{chat_id}",
                        "kind": part.kind,
                        "mime": part.mime,
                        "size_bytes": len(payload),
                    },
                )
                return 1

        return await self._send_text(
            chat_id, describe_undeliverable(part, "Telegram"), reply_to_id
        )

    async def _upload(
        self, chat_id: int | str, part: MediaPart, payload: bytes, reply_to_id: int | None
    ) -> None:
        """Hand the bytes to the API surface that matches the artefact's kind.

        Kind is carried through rather than flattened to "some attachment" — a
        chart posted as a photo renders inline; the same bytes posted as a
        document do not.
        """
        bot = self._application.bot
        if part.kind == "image":
            await bot.send_photo(chat_id=chat_id, photo=payload, reply_to_message_id=reply_to_id)
        elif part.kind == "audio":
            await bot.send_audio(chat_id=chat_id, audio=payload, reply_to_message_id=reply_to_id)
        else:
            await bot.send_document(
                chat_id=chat_id,
                document=payload,
                filename=part.declared_name,
                reply_to_message_id=reply_to_id,
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
    def _resolve_chat_id(chat_id_str: str) -> int | str:
        """Telegram chat ids are numeric; fall back to the raw string (@channel)."""
        try:
            return int(chat_id_str)
        except ValueError:
            return chat_id_str

    # ── Internal: Bot Setup ───────────────────────────────────────────────────

    def _register_handlers(self) -> None:
        """Register message handlers on the Application.

        ``filters.ALL`` and not ``filters.TEXT``: this one line is the reported
        defect (SPEC-065). Under a text-only filter a photo never reached a
        handler, so no run ever started and every per-layer unit test in the
        repo still passed. What an inbound message *is* gets decided in
        :meth:`to_parts`, not in a handler registration.
        """
        # _register_handlers is only called from connect() after _application is set.
        if self._application is None:  # pragma: no cover
            msg = "_register_handlers called before connect()"
            raise RuntimeError(msg)

        from telegram.ext import (
            MessageHandler,
            filters,
        )

        self._application.add_handler(MessageHandler(filters.ALL, self._handle_update))
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
                drop_pending_updates=self._drop_pending_updates,
            )
            # Only the first clean start drops the backlog; a reconnect keeps the
            # messages that queued during the outage instead of silently losing them.
            self._drop_pending_updates = False
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

            # Non-retryable: record fatal state and return. Re-raising here only
            # produces a silent "exception never retrieved" on this un-awaited
            # task; the runner instead observes the death via wait_closed().
            _logger.exception("TelegramAdapter: unhandled error in polling loop: %s", exc)
            self._set_fatal_error(exc, retryable=False)

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
        # A delivered update proves getUpdates is healthy — clear the error streak.
        self._consecutive_update_errors = 0

        if update.effective_message is None or update.effective_user is None:
            return

        user_id: int = update.effective_user.id
        chat_id = str(update.effective_chat.id) if update.effective_chat else str(user_id)

        # Skip our own bot messages to prevent self-talk loops.
        if self._bot_id is not None and user_id == self._bot_id:
            return

        # Auth: static allowed_user_ids OR approved-paired.
        #
        # Static check first (empty allowlist = deny all, fail-closed). On
        # failure: require_pairing=False preserves the original silent-drop
        # behaviour (no reply — avoids confirming bot existence to an
        # attacker). require_pairing=True forwards to on_message instead —
        # SessionRouter's PairingInterceptor makes the final call (mint+DM a
        # pairing code, or route through if `arc gateway pair approve` has
        # already approved this user in the gateway's pairing records).
        if not self._is_authorized(user_id):
            if not self._require_pairing:
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
            _logger.info(
                "TelegramAdapter: user_id=%d not in static allowlist — forwarding for "
                "pairing check (require_pairing=true)",
                user_id,
            )

        parts = self.to_parts(update.effective_message)
        if not parts:
            return

        # Build normalised user DID from Telegram user_id.
        # Full cross-platform identity graph resolution is T1.3;
        # for now we derive a stable platform-scoped DID.
        user_did = f"did:arc:telegram:{user_id}"

        event = InboundDraft(
            platform="telegram",
            chat_id=chat_id,
            thread_id=None,
            user_did=user_did,
            agent_did=self._agent_did,
            parts=parts,
            raw_payload={
                "update_id": update.update_id,
                "user_id": user_id,
                "chat_id": chat_id,
                # Friendly name for arcui's delivery-target dropdown (no PII is
                # persisted elsewhere — pairings/sessions store only hashes).
                "first_name": update.effective_user.first_name,
                "username": update.effective_user.username,
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

    def to_parts(self, payload: Any) -> list[DraftPart]:
        """Turn one Telegram message into ordered parts (REQ-296).

        Words first, then artefacts, so a captioned photo arrives as the sender
        composed it and the order is stable across identical messages. Every
        kind Telegram delivers is represented; nothing is dropped for being
        "not text".

        Args:
            payload: A ``telegram.Message``.

        Returns:
            The parts, empty when the message carries nothing actionable
            (a sticker-less status update, a pinned-message notice).
        """
        parts: list[DraftPart] = []
        text = getattr(payload, "text", None) or getattr(payload, "caption", None)
        if text:
            parts.append(TextPart(text=str(text)))
        parts.extend(self._attachments(payload))
        return parts

    def _attachments(self, message: Any) -> Iterator[PendingMedia]:
        """Name every artefact on a message and how to go and get it.

        Only the *largest* rendition of a photo is taken: Telegram sends the
        same image at four resolutions and the agent wants the one it can read.
        """
        photo = getattr(message, "photo", None)
        if photo:
            yield self._pending(photo[-1], "image", "image/jpeg", "photo.jpg")

        table: tuple[tuple[str, MediaKind | None, str, str], ...] = (
            # A document's kind is its declared mime's business; the rest are
            # what Telegram's own field name already tells us they are.
            ("document", None, "application/octet-stream", "document"),
            ("audio", "audio", "audio/mpeg", "audio.mp3"),
            ("voice", "audio", "audio/ogg", "voice.ogg"),
            ("video", "file", "video/mp4", "video.mp4"),
        )
        for attribute, kind, fallback_mime, fallback_name in table:
            attachment = getattr(message, attribute, None)
            if attachment is None:
                continue
            mime = str(getattr(attachment, "mime_type", None) or fallback_mime)
            yield self._pending(
                attachment,
                kind or kind_for(mime),
                mime,
                str(getattr(attachment, "file_name", None) or fallback_name),
            )

    def _pending(
        self, attachment: Any, kind: MediaKind, mime: str, declared_name: str
    ) -> PendingMedia:
        """Describe one artefact, with a closure that fetches it on demand.

        The bytes are not read here. They are read when the gateway is ready to
        take custody of them, so an artefact over the ceiling is refused against
        the bytes that arrived rather than against the size the payload claimed
        (REQ-299) — and nothing that big ever rides inside the envelope.
        """
        file_id = attachment.file_id
        declared_size = getattr(attachment, "file_size", None)

        async def fetch(limit_bytes: int) -> bytes:
            bot = self._application.bot if self._application is not None else None
            if bot is None:  # pragma: no cover - fetch only runs on a live adapter
                msg = "TelegramAdapter: cannot fetch media before connect()"
                raise RuntimeError(msg)
            handle = await bot.get_file(file_id)
            # get_file restates the size authoritatively; checking it here is
            # free and is the only chance to refuse before the bytes are read.
            size = getattr(handle, "file_size", None)
            if isinstance(size, int) and size > limit_bytes:
                raise MediaTooLargeOnWireError(limit_bytes)
            return bytes(await handle.download_as_bytearray())

        return PendingMedia(
            kind=kind,
            mime=mime,
            declared_name=declared_name,
            fetch=fetch,
            size_bytes=declared_size if isinstance(declared_size, int) else None,
        )

    async def _on_error(self, update: Any, context: Any) -> None:
        """Handle errors from python-telegram-bot update processing.

        PTB delivers getUpdates failures (e.g. a 502 during long-poll) here while
        keeping the poll loop alive, so logging alone means a persistent outage
        never triggers a reconnect. A sustained streak of network/conflict errors
        escalates to a fatal-retryable close so the runner restarts the adapter.
        """
        err = context.error
        _logger.error(
            "TelegramAdapter: update error: %s (update=%s)",
            err,
            update,
            exc_info=err,
        )
        if not (_is_network_error(err) or _is_conflict_error(err)):
            return
        self._consecutive_update_errors += 1
        if self._consecutive_update_errors >= _MAX_CONSECUTIVE_UPDATE_ERRORS:
            _logger.warning(
                "TelegramAdapter: %d consecutive getUpdates failures — escalating to "
                "fatal-retryable so GatewayRunner reconnects. Last error: %s",
                self._consecutive_update_errors,
                err,
            )
            self._set_fatal_error(err, retryable=True)

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
        # Unblock wait_closed() so GatewayRunner observes the death and drives
        # reconnect — the signal the loop previously dropped on the floor.
        self._closed_event.set()
        self._audit(
            _EVENT_FAIL,
            {
                "platform": "telegram",
                "error": str(exc),
                "retryable": retryable,
                "agent_did": self._agent_did,
            },
        )

    async def wait_closed(self) -> tuple[bool, Exception | None]:
        """Block until the event-source loop dies; report the fatal state.

        Returns ``(retryable, error)`` once the polling loop terminates fatally:
        ``retryable`` tells GatewayRunner whether to hand this adapter to the
        reconnect watcher (transient network / poll conflict) or leave it
        permanently failed (bad credentials). A healthy adapter blocks for its
        whole lifetime; connect() re-arms the underlying event on each restart.
        """
        await self._closed_event.wait()
        return self._fatal_retryable, self._fatal_error

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
