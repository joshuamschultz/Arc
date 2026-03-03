"""Slack bot — Socket Mode connection, message handlers, and response delivery.

Manages the Socket Mode WebSocket connection to Slack, routes
inbound DMs to agent.chat(), and delivers responses with smart
splitting at paragraph/sentence boundaries.
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
    from arcagent.modules.slack.config import SlackConfig

_logger = logging.getLogger("arcagent.slack.bot")

# Sentence-ending punctuation for boundary detection
_SENTENCE_END = re.compile(r"[.!?]\s")

# Bot mention pattern for stripping
_BOT_MENTION = re.compile(r"<@[A-Z0-9]+>\s*")


def _user_facing_error(exc: Exception) -> str:
    """Map exceptions to user-friendly error messages.

    Avoids leaking internal details while giving the user
    actionable information about what went wrong.
    """
    # Import here to avoid hard dependency on arcllm from the bot module
    try:
        from arcllm.exceptions import ArcLLMAPIError
    except ImportError:
        return "Error processing your message. Please try again."

    if isinstance(exc, ArcLLMAPIError):
        if exc.status_code == 429:
            return "I'm currently rate limited by the LLM provider. Please try again in a minute or two."
        if exc.status_code in {500, 502, 503}:
            return "The LLM provider is temporarily unavailable. Please try again shortly."
        if exc.status_code == 400 and "content_filter" in exc.body.lower():
            return "Your message was blocked by the content safety filter. Please rephrase and try again."

    if isinstance(exc, TimeoutError):
        return "The request timed out. Please try again with a simpler message."

    return "Error processing your message. Please try again."


def split_message(text: str, max_length: int = 4000) -> list[str]:
    """Split text into chunks respecting natural boundaries.

    Priority order:
    1. Double-newline (paragraph boundary)
    2. Single newline
    3. Sentence boundary (. ! ?)
    4. Hard split at max_length

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


class SlackBot:
    """Manages Socket Mode connection, message routing, and response delivery.

    Uses slack-bolt (async) for Slack API interaction via Socket Mode.
    Messages are processed sequentially via asyncio.Lock (single-user model).
    """

    def __init__(
        self,
        config: SlackConfig,
        telemetry: AgentTelemetry | None = None,
        workspace: Path = Path("."),
    ) -> None:
        self._config = config
        self._telemetry = telemetry
        self._workspace = workspace
        self._agent_chat_fn: Callable[..., Awaitable[Any]] | None = None
        self._lock = asyncio.Lock()
        self._app: Any | None = None
        self._handler: Any | None = None
        self._user_id: str | None = None
        self._current_session_id: str | None = None
        self._dm_channel_id: str | None = None
        self._running = False
        self._file_handler: Any | None = None

        # State persistence path
        self._state_dir = workspace / "slack"
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
        """Start Socket Mode connection in background.

        Reads bot token and app-level token from environment variables.
        If either is not set, logs a warning and stays dormant.
        Validates token prefixes for clear error messages.
        """
        bot_token = os.environ.get(self._config.bot_token_env_var)
        app_token = os.environ.get(self._config.app_token_env_var)

        if not bot_token:
            _logger.warning(
                "Bot token not found in env var '%s'; slack module dormant",
                self._config.bot_token_env_var,
            )
            return

        if not app_token:
            _logger.warning(
                "App-level token not found in env var '%s'; slack module dormant",
                self._config.app_token_env_var,
            )
            return

        # Validate token prefixes
        if not bot_token.startswith("xoxb-"):
            _logger.error("Bot token does not start with 'xoxb-'; check token configuration")
            return

        if not app_token.startswith("xapp-"):
            _logger.error("App-level token does not start with 'xapp-'; check token configuration")
            return

        try:
            from slack_bolt.adapter.socket_mode.async_handler import (  # type: ignore[import-not-found]
                AsyncSocketModeHandler,
            )
            from slack_bolt.async_app import AsyncApp  # type: ignore[import-not-found]
        except ImportError:
            _logger.warning(
                "slack-bolt not installed; slack module dormant. "
                "Install with: pip install 'arcagent[slack]'"
            )
            return

        self._app = AsyncApp(token=bot_token)

        # Register message event handler (catches all DM subtypes)
        @self._app.event("message")  # type: ignore[misc]
        async def handle_message_event(event: dict[str, Any], say: Any) -> None:
            await self._handle_message(event)

        # Register no-op handlers to suppress WARNING logs
        @self._app.event("message_changed")  # type: ignore[misc]
        async def handle_message_changed(event: dict[str, Any]) -> None:
            pass  # Suppress warning logs for message edits

        @self._app.event("message_deleted")  # type: ignore[misc]
        async def handle_message_deleted(event: dict[str, Any]) -> None:
            pass  # Suppress warning logs for message deletions

        # Create Socket Mode handler and connect
        self._handler = AsyncSocketModeHandler(self._app, app_token)
        try:
            await self._handler.connect_async()  # Non-blocking — NOT start_async()
        except Exception:
            _logger.exception("Failed to establish Socket Mode connection")
            self._emit_event("slack:error", {"error": "connection_failed"})
            return

        self._running = True
        self._emit_event("slack:connected", {})
        _logger.info("Slack bot connected via Socket Mode")

    async def stop(self) -> None:
        """Stop Socket Mode, clean up resources."""
        self._running = False

        if self._handler is not None:
            try:
                await self._handler.close_async()  # NOT disconnect_async()
            except Exception:
                _logger.exception("Error closing Socket Mode handler")
            self._handler = None

        self._app = None
        self._emit_event("slack:disconnected", {})
        self._save_state()
        _logger.info("Slack bot stopped")

    async def send_notification(self, text: str) -> None:
        """Send a proactive DM to the stored user."""
        if self._user_id is None:
            _logger.warning("No user_id stored; cannot send notification")
            return

        if self._app is None:
            _logger.warning("Bot not running; cannot send notification")
            return

        channel_id = await self._ensure_dm_channel(self._user_id)
        if channel_id is None:
            return

        chunks = split_message(text, self._config.max_message_length)
        for chunk in chunks:
            await self._app.client.chat_postMessage(channel=channel_id, text=chunk)

        self._emit_event(
            "slack:notification_sent",
            {"user_id": self._user_id, "chunks": len(chunks)},
        )

    # ── Message Handling ─────────────────────────────────────────

    async def _handle_message(self, event: dict[str, Any]) -> None:
        """Route inbound DM events from Slack."""
        # Skip bot messages (prevent infinite loops)
        if event.get("bot_id"):
            return

        user_id = event.get("user", "")
        channel = event.get("channel", "")
        text = event.get("text", "")

        if not user_id:
            return

        # Strip bot mentions
        text = _BOT_MENTION.sub("", text).strip() if text else ""

        # Authorization check
        if not self._is_authorized(user_id):
            _logger.warning(
                "Unauthorized user %s (allowed: %s)",
                user_id,
                self._config.allowed_user_ids,
            )
            self._emit_event("slack:auth_rejected", {"user_id": user_id})
            return

        # Store user_id if not already set (first authorized message)
        if self._user_id is None:
            self._user_id = user_id
            self._dm_channel_id = channel

        # Create session if none exists
        if self._current_session_id is None:
            self._current_session_id = str(uuid.uuid4())
            self._save_state()

        self._emit_event(
            "slack:message_received",
            {
                "user_id": user_id,
                "session_id": self._current_session_id,
            },
        )

        # Handle file attachments
        file_context = await self._handle_files(event)

        # Combine text + file context
        prompt = text
        if file_context:
            prompt = f"{text}\n\n{file_context}" if text else file_context

        # Need either text or files to proceed
        if not prompt:
            return

        # Dispatch text commands (only when no files attached — commands don't have files)
        if not file_context and await self._dispatch_command(text, channel, user_id):
            return

        # Process message through agent.chat()
        async with self._lock:
            await self._process_message(prompt, channel)

    async def _handle_files(self, event: dict[str, Any]) -> str:
        """Download and extract text from Slack file attachments.

        Returns a formatted context string for all files, or empty string.
        """
        files = event.get("files")
        if not files or self._file_handler is None or self._app is None:
            return ""

        bot_token = os.environ.get(self._config.bot_token_env_var, "")
        context_blocks: list[str] = []

        for file_info in files:
            filename = file_info.get("name", "unknown")
            file_size = file_info.get("size", 0)

            # Check allowed extensions
            if self._config.allowed_extensions:
                ext = Path(filename).suffix.lower().lstrip(".")
                if ext not in self._config.allowed_extensions:
                    _logger.info("Skipping file %s — extension not allowed", filename)
                    continue

            # Check file size before downloading
            max_bytes = self._config.max_file_size_mb * 1024 * 1024
            if file_size > max_bytes:
                _logger.info("Skipping file %s — too large (%d bytes)", filename, file_size)
                context_blocks.append(
                    f"*Skipped `{filename}` — exceeds {self._config.max_file_size_mb}MB limit*"
                )
                continue

            # Slack private download URL
            download_url = file_info.get("url_private_download") or file_info.get("url_private")
            if not download_url:
                _logger.debug("No download URL for file %s", filename)
                continue

            # Download with bot token auth
            path = await self._file_handler.download_from_url(
                url=download_url,
                headers={"Authorization": f"Bearer {bot_token}"},
                filename=filename,
            )

            if path is None:
                continue

            # Extract text and build context
            extracted = self._file_handler.extract_text(path)
            context_blocks.append(self._file_handler.build_context(path, extracted))

            self._emit_event(
                "slack:file_received",
                {
                    "filename": filename,
                    "size": file_size,
                    "extracted": extracted is not None,
                    "path": str(path),
                },
            )

        return "\n\n".join(context_blocks)

    async def _dispatch_command(self, text: str, channel: str, user_id: str) -> bool:
        """Dispatch text commands. Returns True if a command was handled."""
        text_lower = text.lower().strip()
        if text_lower == "start":
            await self._handle_start(channel, user_id)
            return True
        if text_lower == "new":
            await self._handle_new(channel, user_id)
            return True
        if text_lower == "status":
            await self._handle_status(channel)
            return True
        return False

    async def _process_message(self, text: str, channel: str) -> None:
        """Process a single message through agent.chat()."""
        if self._app is None:
            return

        if self._agent_chat_fn is None:
            _logger.warning("No agent_chat_fn bound; message skipped")
            await self._app.client.chat_postMessage(
                channel=channel,
                text="Agent not ready — chat function not bound.",
            )
            return

        try:
            result = await self._agent_chat_fn(text, session_id=self._current_session_id)
        except Exception as exc:
            _logger.exception("Error calling agent.chat()")
            error_msg = _user_facing_error(exc)
            await self._app.client.chat_postMessage(
                channel=channel,
                text=error_msg,
            )
            self._emit_event(
                "slack:error",
                {
                    "error": "agent_chat_failed",
                    "error_type": type(exc).__name__,
                    "error_detail": str(exc)[:200],
                },
            )
            return

        # Extract response content
        content = getattr(result, "content", None) or str(result) if result else None
        if not content:
            await self._app.client.chat_postMessage(
                channel=channel,
                text="(No response)",
            )
            return

        # Split and send as regular DM replies
        chunks = split_message(content, self._config.max_message_length)
        for chunk in chunks:
            await self._app.client.chat_postMessage(channel=channel, text=chunk)

        self._emit_event(
            "slack:message_sent",
            {
                "user_id": self._user_id,
                "session_id": self._current_session_id,
                "chunks": len(chunks),
            },
        )

    # ── Text Command Handlers ────────────────────────────────────

    async def _handle_start(self, channel: str, user_id: str) -> None:
        """Handle 'start' text command — bind user and create session."""
        self._user_id = user_id
        self._dm_channel_id = channel
        self._current_session_id = str(uuid.uuid4())
        self._save_state()

        if self._app is not None:
            await self._app.client.chat_postMessage(
                channel=channel,
                text=f"Connected. Session: {self._current_session_id[:8]}...",
            )

    async def _handle_new(self, channel: str, user_id: str) -> None:
        """Handle 'new' text command — create fresh session."""
        self._current_session_id = str(uuid.uuid4())
        self._save_state()

        if self._app is not None:
            await self._app.client.chat_postMessage(
                channel=channel,
                text=f"New session: {self._current_session_id[:8]}...",
            )

    async def _handle_status(self, channel: str) -> None:
        """Handle 'status' text command — show current session info."""
        session_display = self._current_session_id[:8] if self._current_session_id else "none"
        status_text = (
            f"Session: {session_display}...\n"
            f"User: {self._user_id or 'none'}\n"
            f"Connected: {self._running}"
        )
        if self._app is not None:
            await self._app.client.chat_postMessage(
                channel=channel,
                text=status_text,
            )

    # ── DM Channel Management ────────────────────────────────────

    async def _ensure_dm_channel(self, user_id: str) -> str | None:
        """Get or open DM channel with a user. Caches the channel ID."""
        if self._dm_channel_id is not None:
            return self._dm_channel_id

        if self._app is None:
            return None

        try:
            result = await self._app.client.conversations_open(users=user_id)
            self._dm_channel_id = result["channel"]["id"]
            return self._dm_channel_id
        except Exception:
            _logger.exception("Failed to open DM channel with user %s", user_id)
            self._emit_event(
                "slack:error",
                {"error": "conversations_open_failed", "user_id": user_id},
            )
            return None

    # ── Authorization ────────────────────────────────────────────

    def _is_authorized(self, user_id: str) -> bool:
        """Check if user_id is in the allowlist. Empty list = deny all."""
        return user_id in self._config.allowed_user_ids

    # ── State Persistence ────────────────────────────────────────

    def _load_state(self) -> None:
        """Load session state from disk."""
        if self._state_path.exists():
            try:
                data = json.loads(self._state_path.read_text())
                self._user_id = data.get("user_id")
                self._current_session_id = data.get("session_id")
                self._dm_channel_id = data.get("dm_channel_id")
            except (json.JSONDecodeError, OSError):
                _logger.warning("Failed to load slack state; starting fresh")

    def _save_state(self) -> None:
        """Persist session state to disk with restricted permissions."""
        self._state_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "user_id": self._user_id,
            "session_id": self._current_session_id,
            "dm_channel_id": self._dm_channel_id,
        }
        # Write with owner-only permissions (0o600) — user_id is communication metadata
        fd = os.open(str(self._state_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(data, indent=2).encode())
        finally:
            os.close(fd)

    # ── Telemetry ────────────────────────────────────────────────

    def _emit_event(self, event_name: str, data: dict[str, Any]) -> None:
        """Emit a telemetry event if telemetry is available."""
        if self._telemetry is not None and hasattr(self._telemetry, "record_event"):
            self._telemetry.record_event(event_name, data)
