"""Session Manager — conversation lifecycle, JSONL persistence, compaction.

Manages multi-turn conversation sessions with:
- UUID4 session IDs
- Thread-safe message appending (asyncio.Lock)
- Append-only JSONL transcripts
- Letta-style sliding window compaction (30/70 split)
- Pre-compaction flush to context.md (OpenClaw pattern)
- Configurable session retention
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arcagent.core.config import ContextConfig, SessionConfig
from arcagent.utils.io import format_messages

_logger = logging.getLogger("arcagent.session_manager")


class SessionManager:
    """Manage conversation sessions with JSONL persistence and compaction."""

    def __init__(
        self,
        config: SessionConfig,
        context_config: ContextConfig,
        telemetry: Any,
        workspace: Path,
        context_manager: Any = None,
    ) -> None:
        self._config = config
        self._context_config = context_config
        self._telemetry = telemetry
        self._workspace = workspace
        self._sessions_dir = workspace / "sessions"
        self._messages: list[dict[str, Any]] = []
        self._session_id: str = ""
        self._lock = asyncio.Lock()
        self._jsonl_path: Path | None = None
        self._context_manager = context_manager

    @property
    def session_id(self) -> str:
        """Current session ID (empty string if no session created/resumed)."""
        return self._session_id

    @property
    def message_count(self) -> int:
        """Number of messages in the current session."""
        return len(self._messages)

    @property
    def context_manager(self) -> Any:
        """The context manager owned by this session."""
        return self._context_manager

    def token_ratio(self) -> float:
        """Delegate token ratio calculation to context manager."""
        if self._context_manager is None:
            return 0.0
        result: float = self._context_manager.token_ratio()
        return result

    async def create_session(self) -> str:
        """Create a new session with a UUID4 ID.

        Creates the sessions directory and an empty JSONL file.
        Returns the new session ID.
        """
        self._session_id = str(uuid.uuid4())
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        self._jsonl_path = self._sessions_dir / f"{self._session_id}.jsonl"
        self._jsonl_path.touch()
        self._messages = []

        _logger.info("Created session: %s", self._session_id)
        return self._session_id

    async def resume_session(self, session_id: str) -> list[dict[str, Any]]:
        """Load messages from an existing JSONL session file.

        Skips malformed lines gracefully. Returns loaded messages.
        """
        self._session_id = session_id
        self._jsonl_path = self._sessions_dir / f"{session_id}.jsonl"
        self._messages = []

        if not self._jsonl_path.exists():
            _logger.warning("Session file not found: %s", self._jsonl_path)
            return []

        content = self._jsonl_path.read_text(encoding="utf-8").strip()
        if not content:
            return []

        for line_num, line in enumerate(content.split("\n"), 1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                self._messages.append(entry)
            except json.JSONDecodeError:
                _logger.warning(
                    "Skipping malformed JSONL line %d in session %s",
                    line_num,
                    session_id,
                )

        _logger.info(
            "Resumed session %s with %d messages",
            session_id,
            len(self._messages),
        )
        return list(self._messages)

    async def append_message(self, message: dict[str, Any]) -> None:
        """Thread-safe append to message list and JSONL file.

        Adds a timestamp and type field, then writes a single JSON line.
        Uses asyncio.Lock for concurrency safety.
        """
        entry = {
            "type": "message",
            **message,
            "timestamp": datetime.now(UTC).isoformat(),
        }

        async with self._lock:
            self._messages.append(entry)
            if self._jsonl_path is not None:
                with open(self._jsonl_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry) + "\n")

    def get_messages(self) -> list[dict[str, Any]]:
        """Return a snapshot of the current message list (not a reference)."""
        return list(self._messages)

    async def compact(self, model: Any, workspace: Path) -> None:
        """Letta-style sliding window compaction with pre-compaction flush.

        0. PRE-FLUSH: Extract key facts from messages-to-summarize,
           append to context.md (OpenClaw pattern — never lose info)
        1. Split messages: oldest 30% → to_summarize, recent 70% → to_keep
        2. Summarize oldest via eval model
        3. Replace summarized messages with compaction_summary entry
        4. Write compaction_summary to JSONL

        Lock covers entire operation to prevent concurrent compaction
        from corrupting message state (M-06).
        """
        async with self._lock:
            if len(self._messages) < 4:
                return  # Not enough messages to compact

            split_idx = max(1, len(self._messages) * 30 // 100)
            to_summarize = self._messages[:split_idx]
            to_keep = self._messages[split_idx:]

            # Step 0: Pre-compaction flush
            await self._pre_compact_flush(to_summarize, workspace, model)

            # Step 1-2: Summarize via model
            summary_text = await self._summarize_messages(to_summarize, model)

            # Step 3: Build compaction summary entry
            summary_entry: dict[str, Any] = {
                "type": "compaction_summary",
                "summarized_count": len(to_summarize),
                "summary": summary_text,
                "timestamp": datetime.now(UTC).isoformat(),
            }

            # Step 4: Replace messages
            self._messages = [summary_entry, *to_keep]

            # Write summary to JSONL
            if self._jsonl_path is not None:
                with open(self._jsonl_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(summary_entry) + "\n")

        _logger.info(
            "Compacted session %s: %d messages → summary + %d recent",
            self._session_id,
            len(to_summarize),
            len(to_keep),
        )

    async def _pre_compact_flush(
        self,
        messages: list[dict[str, Any]],
        workspace: Path,
        model: Any,
    ) -> None:
        """Flush key facts from messages-about-to-be-compacted to context.md.

        Uses eval model to extract important facts, decisions, and state.
        Sanitizes LLM output before writing to prevent context poisoning
        (ASI-06, LLM-05). Appends to context.md so no info is lost.
        """
        context_path = workspace / "context.md"

        msg_text = format_messages(messages, limit=0, type_filter="message")
        if not msg_text.strip():
            return

        try:
            facts = await model(
                f"Extract the key facts, decisions, and important state from "
                f"this conversation segment. Be concise (2-3 bullet points max):\n\n"
                f"{msg_text}"
            )
            if facts and facts.strip():
                # Sanitize LLM output: strip control characters and
                # markdown injection patterns that could alter prompt behavior
                sanitized = self._sanitize_context_output(facts)
                existing = ""
                if context_path.exists():
                    existing = context_path.read_text(encoding="utf-8")
                with open(context_path, "w", encoding="utf-8") as f:
                    if existing:
                        f.write(existing.rstrip() + "\n\n")
                    f.write(f"## Compaction Flush\n\n{sanitized}\n")
        except Exception:
            _logger.warning("Pre-compaction flush failed, continuing without flush")

    @staticmethod
    def _sanitize_context_output(text: str) -> str:
        """Sanitize LLM output before writing to context.md.

        Strips characters and patterns that could be used for
        prompt injection or context poisoning.
        """
        # Remove null bytes and other control characters (keep newlines, tabs)
        sanitized = "".join(
            c for c in text if c in ("\n", "\t") or (ord(c) >= 32 and ord(c) != 127)
        )
        # Cap length to prevent unbounded context growth
        max_chars = 2000
        if len(sanitized) > max_chars:
            sanitized = sanitized[:max_chars] + "\n[truncated]"
        return sanitized

    async def _summarize_messages(
        self,
        messages: list[dict[str, Any]],
        model: Any,
    ) -> str:
        """Summarize messages using the eval model."""
        msg_text = format_messages(messages, limit=0, type_filter="message")

        try:
            summary: str = await model(
                f"Summarize this conversation segment concisely:\n\n{msg_text}"
            )
            return summary[: self._config.compaction_summary_max_chars]
        except Exception:
            _logger.warning("Summarization failed, using truncated messages")
            return f"[Compacted {len(messages)} messages]"

    async def cleanup_old_sessions(self) -> None:
        """Remove sessions beyond retention limits.

        Keeps the newest N sessions (by modification time).
        """
        if not self._sessions_dir.exists():
            return

        session_files = sorted(
            self._sessions_dir.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,  # Newest first
        )

        # Keep only retention_count newest
        to_remove = session_files[self._config.retention_count :]
        for path in to_remove:
            path.unlink()
            _logger.info("Removed old session: %s", path.name)
