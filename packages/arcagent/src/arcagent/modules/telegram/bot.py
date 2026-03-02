"""Telegram bot — polling loop, message handlers, and response delivery.

Manages the long-polling connection to Telegram Bot API, routes
inbound messages to agent.chat(), and delivers responses with
smart splitting at paragraph/sentence boundaries.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from arcagent.core.telemetry import AgentTelemetry
    from arcagent.modules.telegram.config import TelegramConfig

_logger = logging.getLogger("arcagent.telegram.bot")

# Sentence-ending punctuation for boundary detection
_SENTENCE_END = re.compile(r"[.!?]\s")


def _user_facing_error(exc: Exception) -> str:
    """Map exceptions to user-friendly error messages.

    Avoids leaking internal details while giving the user
    actionable information about what went wrong.
    """
    try:
        from arcllm.exceptions import ArcLLMAPIError
    except ImportError:
        return "Error processing your message. Please try again."

    if isinstance(exc, ArcLLMAPIError) and exc.status_code == 429:
        return "I'm currently rate limited by the LLM provider. Please try again in a minute or two."
    if isinstance(exc, ArcLLMAPIError) and exc.status_code in {500, 502, 503}:
        return "The LLM provider is temporarily unavailable. Please try again shortly."
    return "Error processing your message. Please try again."


def split_message(text: str, max_length: int = 4096) -> list[str]:
    """Split text into chunks respecting natural boundaries.

    Priority order:
    1. Double-newline (paragraph boundary)
    2. Single newline
    3. Sentence boundary (. ! ?)
    4. Hard split at max_length

    Args:
        text: The text to split.
        max_length: Maximum characters per chunk (Telegram limit: 4096).

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

        # Try sentence boundary — find last match without materializing all
        last_match = None
        for m in _SENTENCE_END.finditer(chunk):
            last_match = m
        if last_match is not None:
            split_pos = last_match.end() - 1  # Include punctuation, not space
            chunks.append(remaining[:split_pos])
            remaining = remaining[split_pos:].lstrip()
            continue

        # Hard split — no natural boundary found
        chunks.append(remaining[:max_length])
        remaining = remaining[max_length:]

    return chunks


class TelegramBot:
    """Manages polling loop, message routing, and response delivery.

    Uses python-telegram-bot for the Telegram Bot API interaction.
    Messages are processed sequentially via asyncio.Queue.
    """

    def __init__(
        self,
        config: TelegramConfig,
        telemetry: AgentTelemetry | None = None,
        workspace: Path = Path("."),
    ) -> None:
        self._config = config
        self._telemetry = telemetry
        self._workspace = workspace
        self._agent_chat_fn: Callable[..., Awaitable[Any]] | None = None
        self._message_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._queue_task: asyncio.Task[None] | None = None
        self._application: Any | None = None
        self._chat_id: int | None = None
        self._current_session_id: str | None = None
        self._running = False
        self._bot_id: int | None = None
        self._file_handler: Any | None = None

        # State persistence path
        self._state_dir = workspace / "telegram"
        self._state_path = self._state_dir / "state.json"
        self._load_state()

        # Initialize file handler
        self._init_file_handler()

    def _init_file_handler(self) -> None:
        """Initialize the shared FileHandler for file downloads."""
        try:
            from arcagent.modules.file_handler import FileHandler

            max_bytes = self._config.max_file_size_mb * 1024 * 1024
            self._file_handler = FileHandler(
                workspace=self._workspace,
                max_file_size=max_bytes,
            )
            _logger.debug("FileHandler initialized (max %dMB)", self._config.max_file_size_mb)
        except Exception:
            _logger.debug("FileHandler not available; file uploads will be ignored")
            self._file_handler = None

    def set_agent_chat_fn(self, fn: Callable[..., Awaitable[Any]]) -> None:
        """Bind the agent.chat() callback (deferred binding)."""
        self._agent_chat_fn = fn

    async def start(self) -> None:
        """Start long-polling in a background asyncio task.

        Reads bot token from environment variable. If not set,
        logs a warning and stays dormant.
        """
        token = os.environ.get(self._config.bot_token_env_var)
        if not token:
            _logger.warning(
                "Bot token not found in env var '%s'; telegram module dormant",
                self._config.bot_token_env_var,
            )
            return

        try:
            from telegram import Update  # type: ignore[import-not-found]
            from telegram.ext import (  # type: ignore[import-not-found]
                Application,
                CommandHandler,
                MessageHandler,
                filters,
            )
        except ImportError:
            _logger.warning(
                "python-telegram-bot not installed; telegram module dormant. "
                "Install with: pip install 'arcagent[telegram]'"
            )
            return

        self._application = Application.builder().token(token).build()

        # Register command handlers
        self._application.add_handler(CommandHandler("start", self._handle_start))
        self._application.add_handler(CommandHandler("new", self._handle_new))
        self._application.add_handler(CommandHandler("status", self._handle_status))

        # Text messages (must be before catch-all)
        self._application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )

        # File/attachment handlers — documents, photos, voice, audio, video
        self._application.add_handler(
            MessageHandler(
                filters.Document.ALL | filters.PHOTO | filters.VOICE | filters.AUDIO | filters.VIDEO,
                self._handle_attachment,
            )
        )

        # Catch-all error handler so update errors don't vanish silently
        self._application.add_error_handler(self._on_error)

        # Start the queue processor
        self._running = True
        self._queue_task = asyncio.create_task(self._process_queue())

        # Initialize and start polling — retry on transient network failures
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                await self._application.initialize()
                bot_info = await self._application.bot.get_me()
                break
            except Exception as init_err:
                if attempt == max_retries:
                    _logger.error(
                        "Telegram init failed after %d attempts: %s",
                        max_retries,
                        init_err,
                    )
                    raise
                delay = 2 ** attempt
                _logger.warning(
                    "Telegram init attempt %d/%d failed (%s), retrying in %ds",
                    attempt,
                    max_retries,
                    type(init_err).__name__,
                    delay,
                )
                await asyncio.sleep(delay)

        self._bot_id = bot_info.id
        _logger.info(
            "Telegram bot authenticated: @%s (id=%d)",
            bot_info.username,
            bot_info.id,
        )

        await self._application.start()
        await self._application.updater.start_polling(
            poll_interval=self._config.poll_interval,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )

        self._emit_event("telegram:polling_started", {})
        _logger.info("Telegram bot polling started")

    async def stop(self) -> None:
        """Stop polling, drain the message queue, close connections."""
        self._running = False

        if self._queue_task is not None:
            self._queue_task.cancel()
            try:
                await self._queue_task
            except asyncio.CancelledError:
                pass
            self._queue_task = None

        if self._application is not None:
            if self._application.updater and self._application.updater.running:
                await self._application.updater.stop()
            await self._application.stop()
            await self._application.shutdown()
            self._application = None

        self._emit_event("telegram:polling_stopped", {})
        self._save_state()
        _logger.info("Telegram bot stopped")

    async def send_notification(self, text: str) -> None:
        """Send a proactive message to the stored chat_id."""
        if self._chat_id is None:
            _logger.warning("No chat_id stored; cannot send notification")
            return

        if self._application is None:
            _logger.warning("Bot not running; cannot send notification")
            return

        chunks = split_message(text, self._config.max_message_length)
        for chunk in chunks:
            await self._application.bot.send_message(
                chat_id=self._chat_id,
                text=chunk,
            )

        self._emit_event(
            "telegram:notification_sent",
            {"chat_id": self._chat_id, "chunks": len(chunks)},
        )

    # ── Command Handlers ──────────────────────────────────────────

    async def _handle_start(self, update: Any, context: Any) -> None:
        """Handle /start — bind chat and create session."""
        if update.effective_chat is None:
            return

        chat_id = update.effective_chat.id

        if not self._is_authorized(chat_id):
            self._emit_event("telegram:auth_rejected", {"chat_id": chat_id})
            return

        self._chat_id = chat_id
        self._current_session_id = str(uuid.uuid4())
        self._save_state()

        await update.message.reply_text(
            f"Connected. Session: {self._current_session_id[:8]}..."
        )
        self._emit_event(
            "telegram:message_received",
            {
                "chat_id": chat_id,
                "command": "/start",
                "session_id": self._current_session_id,
            },
        )

    async def _handle_new(self, update: Any, context: Any) -> None:
        """Handle /new — create fresh session."""
        if update.effective_chat is None:
            return

        chat_id = update.effective_chat.id

        if not self._is_authorized(chat_id):
            self._emit_event("telegram:auth_rejected", {"chat_id": chat_id})
            return

        self._current_session_id = str(uuid.uuid4())
        self._save_state()

        await update.message.reply_text(f"New session: {self._current_session_id[:8]}...")
        self._emit_event(
            "telegram:message_received",
            {
                "chat_id": chat_id,
                "command": "/new",
                "session_id": self._current_session_id,
            },
        )

    async def _handle_status(self, update: Any, context: Any) -> None:
        """Handle /status — show current session info."""
        if update.effective_chat is None:
            return

        chat_id = update.effective_chat.id

        if not self._is_authorized(chat_id):
            self._emit_event("telegram:auth_rejected", {"chat_id": chat_id})
            return

        status_text = (
            f"Session: {self._current_session_id[:8] if self._current_session_id else 'none'}...\n"
            f"Chat ID: {chat_id}\n"
            f"Queue: {self._message_queue.qsize()} pending"
        )
        await update.message.reply_text(status_text)

    # ── Message Handling ──────────────────────────────────────────

    async def _handle_message(self, update: Any, context: Any) -> None:
        """Handle free-text messages — route to agent.chat()."""
        if update.effective_chat is None or update.message is None:
            return

        chat_id = update.effective_chat.id
        _logger.debug("Received message from chat_id=%d", chat_id)

        # Skip own bot messages (prevent self-talk loops)
        if update.message.from_user and self._bot_id and update.message.from_user.id == self._bot_id:
            return

        if not self._is_authorized(chat_id):
            _logger.warning(
                "Unauthorized chat_id %d (allowed: %s)",
                chat_id,
                self._config.allowed_chat_ids,
            )
            self._emit_event("telegram:auth_rejected", {"chat_id": chat_id})
            return

        text = update.message.text
        if not text:
            return

        # Store chat_id if not already set
        if self._chat_id is None:
            self._chat_id = chat_id

        # Create session if none exists
        if self._current_session_id is None:
            self._current_session_id = str(uuid.uuid4())
            self._save_state()

        # Send typing indicator
        await update.effective_chat.send_action("typing")

        self._emit_event(
            "telegram:message_received",
            {
                "chat_id": chat_id,
                "session_id": self._current_session_id,
            },
        )

        # Enqueue for sequential processing
        await self._message_queue.put(
            {
                "text": text,
                "chat_id": chat_id,
                "update": update,
            }
        )

    async def _handle_attachment(self, update: Any, context: Any) -> None:
        """Handle file/photo/voice/audio/video attachments."""
        if update.effective_chat is None or update.message is None:
            return

        chat_id = update.effective_chat.id

        # Skip own bot messages
        if update.message.from_user and self._bot_id and update.message.from_user.id == self._bot_id:
            return

        if not self._is_authorized(chat_id):
            self._emit_event("telegram:auth_rejected", {"chat_id": chat_id})
            return

        if self._file_handler is None:
            _logger.debug("FileHandler not available; ignoring attachment")
            return

        # Store chat_id if not already set
        if self._chat_id is None:
            self._chat_id = chat_id

        # Create session if none exists
        if self._current_session_id is None:
            self._current_session_id = str(uuid.uuid4())
            self._save_state()

        await update.effective_chat.send_action("typing")

        # Collect file context from all attachment types
        file_context = await self._download_telegram_files(update.message)

        # Caption text (user can add text with a file)
        caption = update.message.caption or ""
        prompt = f"{caption}\n\n{file_context}" if caption else file_context

        if not prompt:
            return

        self._emit_event(
            "telegram:message_received",
            {
                "chat_id": chat_id,
                "session_id": self._current_session_id,
                "has_attachment": True,
            },
        )

        # Enqueue for sequential processing
        await self._message_queue.put(
            {
                "text": prompt,
                "chat_id": chat_id,
                "update": update,
            }
        )

    async def _download_telegram_files(self, message: Any) -> str:
        """Download files from a Telegram message and build context.

        Handles: document, photo, voice, audio, video.
        Returns formatted context string.
        """
        context_blocks: list[str] = []

        # Document (files with any extension)
        if message.document:
            ctx = await self._download_single_telegram_file(
                file_id=message.document.file_id,
                filename=message.document.file_name or "document",
                file_size=message.document.file_size or 0,
            )
            if ctx:
                context_blocks.append(ctx)

        # Photo (get largest resolution — last in the list)
        if message.photo:
            photo = message.photo[-1]  # Largest size
            ctx = await self._download_single_telegram_file(
                file_id=photo.file_id,
                filename=f"photo_{photo.file_unique_id}.jpg",
                file_size=photo.file_size or 0,
            )
            if ctx:
                context_blocks.append(ctx)

        # Voice message
        if message.voice:
            ctx = await self._download_single_telegram_file(
                file_id=message.voice.file_id,
                filename=f"voice_{message.voice.file_unique_id}.ogg",
                file_size=message.voice.file_size or 0,
            )
            if ctx:
                context_blocks.append(ctx)

        # Audio file
        if message.audio:
            ctx = await self._download_single_telegram_file(
                file_id=message.audio.file_id,
                filename=message.audio.file_name or f"audio_{message.audio.file_unique_id}.mp3",
                file_size=message.audio.file_size or 0,
            )
            if ctx:
                context_blocks.append(ctx)

        # Video
        if message.video:
            ctx = await self._download_single_telegram_file(
                file_id=message.video.file_id,
                filename=message.video.file_name or f"video_{message.video.file_unique_id}.mp4",
                file_size=message.video.file_size or 0,
            )
            if ctx:
                context_blocks.append(ctx)

        return "\n\n".join(context_blocks)

    async def _download_single_telegram_file(
        self,
        file_id: str,
        filename: str,
        file_size: int,
    ) -> str | None:
        """Download a single Telegram file and return its context string."""
        if self._application is None or self._file_handler is None:
            return None

        # Check allowed extensions
        if self._config.allowed_extensions:
            ext = Path(filename).suffix.lower().lstrip(".")
            if ext not in self._config.allowed_extensions:
                _logger.info("Skipping file %s — extension not allowed", filename)
                return None

        # Check file size
        max_bytes = self._config.max_file_size_mb * 1024 * 1024
        if file_size > max_bytes:
            _logger.info("Skipping file %s — too large (%d bytes)", filename, file_size)
            return f"*Skipped `{filename}` — exceeds {self._config.max_file_size_mb}MB limit*"

        try:
            tg_file = await self._application.bot.get_file(file_id)
            data = await tg_file.download_as_bytearray()

            path = await self._file_handler.download_from_bytes(bytes(data), filename)
            if path is None:
                return None

            extracted = self._file_handler.extract_text(path)
            context = self._file_handler.build_context(path, extracted)

            self._emit_event(
                "telegram:file_received",
                {
                    "filename": filename,
                    "size": file_size,
                    "extracted": extracted is not None,
                    "path": str(path),
                },
            )

            return context

        except Exception:
            _logger.exception("Failed to download Telegram file: %s", filename)
            return None

    # ── Queue Processing ──────────────────────────────────────────

    async def _process_queue(self) -> None:
        """Dequeue and process messages sequentially."""
        while self._running:
            try:
                item = await asyncio.wait_for(self._message_queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            try:
                await self._process_message(item)
            except Exception as exc:  # Broad catch intentional — queue worker must not crash
                _logger.exception("Error processing telegram message")
                self._emit_event(
                    "telegram:error",
                    {
                        "chat_id": item.get("chat_id"),
                        "error": "processing_failed",
                    },
                )
                # Best-effort error notification to user
                update = item.get("update")
                if update and update.message:
                    try:
                        await update.message.reply_text(
                            _user_facing_error(exc),
                        )
                    except Exception:  # Best-effort notification must not crash queue
                        _logger.exception("Failed to send error reply")
            finally:
                self._message_queue.task_done()

    async def _process_message(self, item: dict[str, Any]) -> None:
        """Process a single queued message through agent.chat()."""
        text = item["text"]
        update = item["update"]

        if self._agent_chat_fn is None:
            _logger.warning("No agent_chat_fn bound; message skipped")
            await update.message.reply_text("Agent not ready — chat function not bound.")
            return

        result = await self._agent_chat_fn(text, session_id=self._current_session_id)

        # Extract response content
        content = getattr(result, "content", None) or str(result) if result else None
        if not content:
            await update.message.reply_text("(No response)")
            return

        # Split and send
        chunks = split_message(content, self._config.max_message_length)
        for chunk in chunks:
            await update.message.reply_text(chunk)

        self._emit_event(
            "telegram:message_sent",
            {
                "chat_id": item["chat_id"],
                "session_id": self._current_session_id,
                "chunks": len(chunks),
            },
        )

    # ── Error Handling ─────────────────────────────────────────────

    async def _on_error(self, update: Any, context: Any) -> None:
        """Log errors from python-telegram-bot update processing."""
        _logger.error(
            "Telegram update error: %s (update=%s)",
            context.error,
            update,
            exc_info=context.error,
        )

    # ── Authorization ─────────────────────────────────────────────

    def _is_authorized(self, chat_id: int) -> bool:
        """Check if chat_id is in the allowlist. Empty list = deny all."""
        return chat_id in self._config.allowed_chat_ids

    # ── State Persistence ─────────────────────────────────────────

    def _load_state(self) -> None:
        """Load session state from disk."""
        if self._state_path.exists():
            try:
                data = json.loads(self._state_path.read_text())
                self._chat_id = data.get("chat_id")
                self._current_session_id = data.get("session_id")
            except (json.JSONDecodeError, OSError):
                _logger.warning("Failed to load telegram state; starting fresh")

    def _save_state(self) -> None:
        """Persist session state to disk with restricted permissions."""
        self._state_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "chat_id": self._chat_id,
            "session_id": self._current_session_id,
        }
        # Write with owner-only permissions (0o600) — chat_id is communication metadata
        fd = os.open(str(self._state_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(data, indent=2).encode())
        finally:
            os.close(fd)

    # ── Telemetry ─────────────────────────────────────────────────

    def _emit_event(self, event_name: str, data: dict[str, Any]) -> None:
        """Emit a telemetry event if telemetry is available."""
        if self._telemetry is not None and hasattr(self._telemetry, "record_event"):
            self._telemetry.record_event(event_name, data)
