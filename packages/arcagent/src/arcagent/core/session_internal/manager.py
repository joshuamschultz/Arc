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

import arcrun
from arcprompt import load_stock

from arcagent.core.config import ContextConfig, SessionConfig
from arcagent.utils.io import format_messages
from arcagent.utils.sanitizer import sanitize_text

if TYPE_CHECKING:
    from arcagent.core.session_internal.context import ContextManager

_logger = logging.getLogger("arcagent.session_manager")
_MAX_REPLAY_FILE_BYTES = 64 * 1024 * 1024
_MAX_REPLAY_LINE_BYTES = 2 * 1024 * 1024
_MAX_REPLAY_RECORDS = 100_000


def _load_session_records(
    path: Path, session_id: str
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Synchronously stream and validate one bounded session journal."""
    size = path.stat().st_size
    if size > _MAX_REPLAY_FILE_BYTES:
        raise ValueError(f"session file exceeds {_MAX_REPLAY_FILE_BYTES} bytes")

    messages: list[dict[str, Any]] = []
    checkpoint: dict[str, Any] | None = None
    with open(path, "rb") as stream:
        for line_num, raw_line in enumerate(stream, 1):
            if line_num > _MAX_REPLAY_RECORDS:
                raise ValueError(f"session exceeds {_MAX_REPLAY_RECORDS} records")
            if len(raw_line) > _MAX_REPLAY_LINE_BYTES:
                raise ValueError(f"session line {line_num} exceeds size limit")
            if not raw_line.endswith(b"\n"):
                _logger.warning("Ignoring incomplete JSONL tail in session %s", session_id)
                break
            try:
                entry = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                _logger.warning(
                    "Skipping malformed JSONL line %d in session %s", line_num, session_id
                )
                continue
            if not isinstance(entry, dict):
                _logger.warning(
                    "Skipping non-object JSONL line %d in session %s", line_num, session_id
                )
                continue
            if entry.get("type") == "checkpoint":
                checkpoint = entry
                continue
            if entry.get("type") == "compaction_boundary":
                baseline = entry.get("messages")
                if not isinstance(baseline, list):
                    _logger.warning("Skipping invalid compaction boundary in %s", session_id)
                    continue
                validated: list[dict[str, Any]] = []
                for message in baseline:
                    if not isinstance(message, dict):
                        validated = []
                        break
                    try:
                        arcrun.Message.model_validate(
                            {"role": message.get("role"), "content": message.get("content")}
                        )
                    except Exception:
                        validated = []
                        break
                    validated.append(message)
                if validated:
                    messages = validated
                else:
                    _logger.warning("Skipping invalid compaction baseline in %s", session_id)
                continue
            if entry.get("type") not in {"message", "compaction_summary"}:
                _logger.warning(
                    "Skipping unknown session record at line %d in %s", line_num, session_id
                )
                continue
            try:
                arcrun.Message.model_validate(
                    {"role": entry.get("role"), "content": entry.get("content")}
                )
            except Exception:
                _logger.warning(
                    "Skipping invalid message at line %d in session %s", line_num, session_id
                )
                continue
            messages.append(entry)
    return messages, checkpoint


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
        self._revision = 0

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

        self._messages, self._last_checkpoint = await asyncio.to_thread(
            _load_session_records, self._jsonl_path, session_id
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
            self._revision += 1
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

    async def prune(self) -> None:
        """Discrete observation-masking — the 70-85% band of the S001 SDD ladder.

        Replace stale tool outputs with placeholders to reclaim context WITHOUT an
        LLM summary — the lighter step below the compaction threshold. Like
        compaction it is a DISCRETE, persisted boundary (not a per-turn rewrite),
        so the reclaimed baseline stays cache-stable rather than busting the
        prompt cache every turn; the recent window is protected. Idempotent: a
        repeated prune finds nothing new to mask and no-ops.
        """
        if self._context_manager is None:
            return
        async with self._lock:
            if len(self._messages) < 4:
                return
            snapshot = list(self._messages)
            snapshot_revision = self._revision
            before = len(snapshot)

        protected = int(self._context_config.max_tokens * 0.40)
        masked = self._context_manager.prune_observations(
            snapshot, protected_recent_tokens=protected
        )
        if masked == snapshot:
            return  # nothing stale to mask

        async with self._lock:
            if self._revision != snapshot_revision:
                if self._telemetry is not None:
                    self._telemetry.audit_event(
                        "context.prune_skipped",
                        {"session_id": self._session_id, "reason": "concurrent_append"},
                    )
                return
            self._messages = list(masked)
            self._revision += 1
            if self._jsonl_path is not None:
                with open(self._jsonl_path, "a", encoding="utf-8") as f:
                    f.write(
                        json.dumps(
                            {
                                "type": "compaction_boundary",
                                "messages": masked,
                                "timestamp": datetime.now(UTC).isoformat(),
                            }
                        )
                        + "\n"
                    )
            if self._telemetry is not None:
                self._telemetry.audit_event(
                    "context.prune",
                    {"session_id": self._session_id, "messages": before},
                )
        _logger.info("Pruned observations in session %s (%d messages)", self._session_id, before)

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

        The message snapshot is taken under the lock, model work happens outside
        it, and a revision compare-and-swap prevents a stale summary from
        overwriting concurrent appends.
        """
        async with self._lock:
            if len(self._messages) < 4:
                return  # Not enough messages to compact
            snapshot = list(self._messages)
            snapshot_revision = self._revision
            messages_before = len(self._messages)
            to_summarize, to_keep = self._split_for_compaction(snapshot)

        try:
            summary_text = await self._summarize_messages(to_summarize, model)
        except TimeoutError:
            _logger.warning(
                "Compaction summarizer timed out after %.1fs; skipping for session %s",
                self._config.compaction_timeout_seconds,
                self._session_id,
            )
            if self._telemetry is not None:
                self._telemetry.audit_event(
                    "context.compaction_skipped",
                    {"session_id": self._session_id, "reason": "summarizer_timeout"},
                )
            return

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
        baseline = [summary_entry, *to_keep]

        async with self._lock:
            if self._revision != snapshot_revision:
                if self._telemetry is not None:
                    self._telemetry.audit_event(
                        "context.compaction_skipped",
                        {"session_id": self._session_id, "reason": "concurrent_append"},
                    )
                return
            self._messages = [summary_entry, *to_keep]
            self._revision += 1
            messages_after = len(self._messages)

            if self._jsonl_path is not None:
                with open(self._jsonl_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(summary_entry) + "\n")
                    f.write(
                        json.dumps(
                            {
                                "type": "compaction_boundary",
                                "messages": baseline,
                                "timestamp": datetime.now(UTC).isoformat(),
                            }
                        )
                        + "\n"
                    )

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

    def _split_for_compaction(
        self, messages: list[dict[str, Any]] | None = None
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Deep token-based split, delegating token math to the context manager.

        Falls back to a 30/70 count split when no context manager is wired
        (test-only construction; production always injects one).
        """
        if self._context_manager is not None:
            return self._context_manager.compaction_split(messages or self._messages)
        source = messages or self._messages
        split_idx = max(1, len(source) * 30 // 100)
        return source[:split_idx], source[split_idx:]

    @staticmethod
    def _sanitize_context_output(text: str) -> str:
        """Sanitize LLM output before it re-enters the message baseline (ASI-06).

        Shared defense-in-depth sanitizer: NFKC + strip zero-width/invisible
        (incl. tag block + variation selectors) + strip control chars + cap.
        """
        return sanitize_text(text, max_length=2000, truncation_suffix="\n[truncated]")

    async def _summarize_messages(
        self,
        messages: list[dict[str, Any]],
        model: Any,
    ) -> str:
        """Summarize messages into the structured schema via the eval model."""
        msg_text = format_messages(messages, limit=0, type_filter="message")
        summary_template = load_stock("arcagent", "summary_template")

        try:
            result = await arcrun.run_oneshot(
                model,
                system=summary_template,
                user=msg_text,
                max_tokens=None,
                timeout=self._config.compaction_timeout_seconds,
            )
            summary: str = result.content or ""
            return summary[: self._config.compaction_summary_max_chars]
        except TimeoutError:
            # Distinct from a provider error: the caller skips compaction entirely
            # so a hung call never wedges the turn (or drops its reply).
            raise
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
