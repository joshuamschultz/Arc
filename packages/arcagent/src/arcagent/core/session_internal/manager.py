"""Session Manager — conversation lifecycle, JSONL persistence, compaction.

Manages multi-turn conversation sessions with:
- UUID4 session IDs
- Thread-safe message appending (asyncio.Lock)
- Append-only JSONL transcripts
- Discrete, persisted compaction: deep token-based split + structured summary
  + boundary observation-masking, written back as a new baseline
- Configurable session retention

Compaction manages message history ONLY. Durable curation of ``context.md`` is
owned solely by the workpad module (:mod:`arcagent.modules.workpad`); compaction
does not write that file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arcllm.types import Message

from arcagent.core.config import ContextConfig, SessionConfig
from arcagent.utils.io import format_messages
from arcagent.utils.sanitizer import sanitize_text

if TYPE_CHECKING:
    from arcagent.core.session_internal.context import ContextManager

_logger = logging.getLogger("arcagent.session_manager")


class SessionManager:
    """Manage conversation sessions with JSONL persistence and compaction."""

    def __init__(
        self,
        config: SessionConfig,
        context_config: ContextConfig,
        telemetry: Any,
        workspace: Path,
        context_manager: ContextManager | None = None,
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
        # SPEC-043 REQ-005 — last persisted loop checkpoint record (metadata
        # only). Kept out of the message list so it never re-enters the model
        # transcript; a resume rebuilds RunState from it + the transcript.
        self._last_checkpoint: dict[str, Any] | None = None

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
        """Provider-reported usage ratio (accumulator). See context_ratio()."""
        if self._context_manager is None:
            return 0.0
        return self._context_manager.token_ratio()

    def context_ratio(self) -> float:
        """Estimated fill ratio of the CURRENT context (tokens / max_tokens).

        The signal used to trigger compaction: an estimate over the live
        message list — the honest measure of how full context is right now,
        and it drops after a compaction boundary so the trigger debounces
        naturally. (The reported-usage accumulator is not wired end-to-end
        and never resets, so it is unsuitable as a trigger — SPEC-029 review.)
        """
        if self._context_manager is None:
            return 0.0
        return self._context_manager.message_fill_ratio(self._messages)

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

    def _session_jsonl_path(self, key: str) -> Path:
        """Resolve ``<sessions>/<key>.jsonl``, rejecting keys that escape the dir.

        Session keys are caller-supplied (channel ids, CLI keys, and crucially
        workspace-authored scheduler/pulse job names) and become a filename, so
        an unvalidated key like ``../../etc/x`` would be an out-of-tree write.
        Reject path separators / traversal / NUL and assert containment under
        ``sessions_dir`` (fail-closed).
        """
        if not key or "/" in key or "\\" in key or "\x00" in key or key in (".", ".."):
            msg = f"invalid session key: {key!r}"
            raise ValueError(msg)
        candidate = self._sessions_dir / f"{key}.jsonl"
        sessions_root = self._sessions_dir.resolve()
        if sessions_root != candidate.resolve().parent:
            msg = f"session key escapes the sessions directory: {key!r}"
            raise ValueError(msg)
        return candidate

    async def resume_session(self, session_id: str) -> list[dict[str, Any]]:
        """Load messages from an existing JSONL session file.

        Skips malformed lines gracefully. Returns loaded messages.
        """
        self._session_id = session_id
        self._jsonl_path = self._session_jsonl_path(session_id)
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
                # Checkpoint records are loop metadata, not conversation — keep
                # them out of the transcript (they are not valid Messages) and
                # retain only the latest for a possible resume (REQ-003/005).
                if entry.get("type") == "checkpoint":
                    self._last_checkpoint = entry
                    continue
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

    async def open_or_resume(self, key: str) -> list[dict[str, Any]]:
        """Bind this manager to ``key``, resuming history if it exists.

        Deterministic by key: a returning conversation (same channel, same CLI
        key) reloads its prior messages; a first-seen key starts an empty,
        persisted session. This is how sessionless surfaces (CLI, scheduler) and
        channel surfaces alike get a stable session from the agent's pool.
        """
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = self._session_jsonl_path(key)
        if jsonl_path.exists():
            return await self.resume_session(key)
        self._session_id = key
        self._jsonl_path = jsonl_path
        self._jsonl_path.touch()
        self._messages = []
        _logger.info("Opened session: %s", key)
        return []

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

    async def persist_checkpoint(self, checkpoint: Any, *, signature: str | None = None) -> None:
        """Persist an arcrun ``LoopCheckpoint`` as one JSONL line (SPEC-043 REQ-005).

        arcrun emits the checkpoint at each turn boundary; arcagent persists it.
        Only the scalar metadata (``to_record()``) is written — the transcript is
        already the durable session content on this same JSONL, so resume rebuilds
        the message list from the transcript and the checkpoint carries no inline
        duplicate (OQ-4). Written under the existing append lock (append-only). The
        operator ``signature`` (SPEC-043 F3) rides the record so resume can refuse
        a tampered/unsigned checkpoint fail-closed.
        """
        record = {
            "type": "checkpoint",
            **checkpoint.to_record(),
            "timestamp": datetime.now(UTC).isoformat(),
            "signature": signature,
        }
        async with self._lock:
            self._last_checkpoint = record
            if self._jsonl_path is not None:
                with open(self._jsonl_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record) + "\n")
        if self._telemetry is not None:
            self._telemetry.audit_event(
                "loop.checkpoint",
                {
                    "session_id": self._session_id,
                    "turn_count": record.get("turn_count"),
                    "run_id": record.get("run_id"),
                },
            )

    def latest_checkpoint(self) -> dict[str, Any] | None:
        """Return the most recent persisted checkpoint record, or None (REQ-003)."""
        return self._last_checkpoint

    async def compact(self, model: Any) -> None:
        """Discrete, persisted compaction (SPEC-029 D-396/398/399/400).

        Append-only between boundaries; when this fires it performs ONE deep,
        debounced compaction and writes the result back as the new baseline:

        1. DEEP SPLIT: keep a recent tail (~<=45% of max_tokens), summarize
           the rest, so the post-compaction ratio lands near half the window
           and many append-only turns follow before the next boundary.
        2. STRUCTURED SUMMARY of the old segment via the eval model (schema,
           not prose — structure forces preservation).
        3. OBSERVATION MASKING of stale tool outputs in the kept window,
           persisted into the rebuilt list (keep tool-call metadata).
        4. Rebuild ``[summary_entry, *masked_kept]``; emit an audit event.

        Durable state is preserved in the structured summary that re-enters the
        message baseline; ``context.md`` is NOT touched here (the workpad module
        owns it — separation of concerns).

        Lock covers the whole operation to prevent concurrent compaction from
        corrupting message state (M-06).
        """
        async with self._lock:
            if len(self._messages) < 4:
                return  # Not enough messages to compact

            messages_before = len(self._messages)
            to_summarize, to_keep = self._split_for_compaction()

            summary_text = await self._summarize_messages(to_summarize, model)

            # Observation masking on the kept window: keep tool-call metadata,
            # replace stale output bodies with placeholders. Persisted below so
            # it is not re-derived (and cache-busted) every subsequent turn.
            if self._context_manager is not None:
                protected = int(self._context_config.max_tokens * 0.20)
                to_keep = self._context_manager.prune_observations(
                    to_keep, protected_recent_tokens=protected
                )

            # role/content make the entry render as a normal prefix message on
            # reassembly (agent_dispatch builds history via Message(**record));
            # `type` + counts are metadata (pydantic ignores extra keys). The
            # summary is sanitized before it re-enters context, mirroring the
            # context.md flush (ASI-06 — no injection laundering into the baseline).
            safe_summary = self._sanitize_context_output(summary_text)
            summary_entry: dict[str, Any] = {
                "type": "compaction_summary",
                "role": "user",
                "content": f"[Summary of {len(to_summarize)} earlier messages]\n{safe_summary}",
                "summarized_count": len(to_summarize),
                "timestamp": datetime.now(UTC).isoformat(),
            }
            self._messages = [summary_entry, *to_keep]
            messages_after = len(self._messages)

            if self._jsonl_path is not None:
                with open(self._jsonl_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(summary_entry) + "\n")

            if self._telemetry is not None:
                self._telemetry.audit_event(
                    "context.compaction",
                    {
                        "session_id": self._session_id,
                        "messages_before": messages_before,
                        "messages_after": messages_after,
                        "summarized_count": len(to_summarize),
                    },
                )

        _logger.info(
            "Compacted session %s: %d messages -> summary + %d recent",
            self._session_id,
            len(to_summarize),
            len(to_keep),
        )

    def _split_for_compaction(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Deep token-based split, delegating token math to the context manager.

        Falls back to a 30/70 count split when no context manager is wired
        (test-only construction; production always injects one).
        """
        if self._context_manager is not None:
            return self._context_manager.compaction_split(self._messages)
        split_idx = max(1, len(self._messages) * 30 // 100)
        return self._messages[:split_idx], self._messages[split_idx:]

    @staticmethod
    def _sanitize_context_output(text: str) -> str:
        """Sanitize LLM output before it re-enters the message baseline (ASI-06).

        Shared defense-in-depth sanitizer: NFKC + strip zero-width/invisible
        (incl. tag block + variation selectors) + strip control chars + cap.
        """
        return sanitize_text(text, max_length=2000, truncation_suffix="\n[truncated]")

    # Structured schema for compaction summaries. Fields (not free prose) force
    # the model to preserve actionable state; `goal`/`constraints` are copied
    # verbatim (they carry security-relevant instructions — LLM07/ASI01). The
    # two fields research flags as most-dropped-yet-critical are kept explicit:
    # `rejected_approaches` (prevents repeated dead ends) and a quantified
    # `progress` (prevents premature "done").
    _SUMMARY_TEMPLATE = (
        "Summarize this conversation segment into the EXACT template below. "
        "Copy `goal` and `constraints` VERBATIM (do not paraphrase). Use concise "
        "bullets; leave a field blank only if truly empty. Do not invent facts.\n\n"
        "goal: <original task, verbatim>\n"
        "constraints: <security/user constraints, verbatim>\n"
        "progress: <what is done so far, quantified>\n"
        "key_facts: <durable facts learned, with provenance>\n"
        "files_modified: <path: one-line change>\n"
        "decisions: <decision: rationale>\n"
        "rejected_approaches: <what was tried and failed, and why>\n"
        "open_questions: <unresolved items blocking completion>\n"
        "next_step: <single concrete next action>\n\n"
        "CONVERSATION SEGMENT:\n"
    )

    async def _summarize_messages(
        self,
        messages: list[dict[str, Any]],
        model: Any,
    ) -> str:
        """Summarize messages into the structured schema via the eval model."""
        msg_text = format_messages(messages, limit=0, type_filter="message")

        try:
            response = await model.invoke(
                [Message(role="user", content=self._SUMMARY_TEMPLATE + msg_text)]
            )
            summary: str = response.content or ""
            return summary[: self._config.compaction_summary_max_chars]
        except Exception:  # reason: fail-open — log + continue
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
