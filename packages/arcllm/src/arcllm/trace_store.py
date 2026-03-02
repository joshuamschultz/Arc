"""TraceStore — append-only, hash-chained LLM call recording.

TraceRecord captures everything about a single LLM invoke():
timing, tokens, cost, request/response bodies, and phase sub-timings.

Records are hash-chained (SHA-256) for tamper-evident audit trails.
JSONLTraceStore implements the TraceStore Protocol with daily rotation.
"""

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import jcs  # type: ignore[import-untyped]  # RFC 8785 canonical JSON — no stubs available
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TraceRecord — frozen Pydantic model for a single LLM call
# ---------------------------------------------------------------------------


class TraceRecord(BaseModel, frozen=True):
    """Single LLM call record. Immutable, hashable, serializable."""

    trace_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    provider: str
    model: str
    agent_label: str | None = None
    agent_did: str | None = None
    budget_scope: str | None = None

    # Request body (optional per tier config)
    request_body: dict[str, Any] | None = None

    # Response body (optional per tier config)
    response_body: dict[str, Any] | None = None

    # Telemetry (always present for llm_call events)
    duration_ms: float = 0.0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    stop_reason: str = "end_turn"
    status: Literal["success", "error", "timeout"] = "success"
    error: str | None = None

    # Retry tracking (links attempts in a retry sequence)
    attempt_number: int = 0
    retry_group_id: str | None = None

    # Sub-phase timing (key → milliseconds)
    phase_timings: dict[str, float] = Field(default_factory=dict)

    # Event type discriminator
    event_type: Literal[
        "llm_call", "config_change", "circuit_change", "rotation"
    ] = "llm_call"
    event_data: dict[str, Any] | None = None

    # Hash chain
    prev_hash: str = "0" * 64
    record_hash: str = ""

    def compute_hash(self) -> str:
        """Compute SHA-256 hash of this record using JCS canonical JSON.

        Hash covers all fields EXCEPT record_hash itself, plus prev_hash.
        """
        data = self.model_dump(exclude={"record_hash"})
        canonical = jcs.canonicalize(data)
        return hashlib.sha256(canonical).hexdigest()

    def with_hash(self, prev_hash: str) -> "TraceRecord":
        """Return a new record with prev_hash set and record_hash computed."""
        updated = self.model_copy(update={"prev_hash": prev_hash})
        computed = updated.compute_hash()
        return updated.model_copy(update={"record_hash": computed})


# ---------------------------------------------------------------------------
# TraceStore Protocol
# ---------------------------------------------------------------------------


class TraceStore(Protocol):
    """Protocol for trace persistence backends."""

    async def append(self, record: TraceRecord) -> None:
        """Append a record to the store. Computes hash chain automatically."""
        ...

    async def query(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        provider: str | None = None,
        agent: str | None = None,
        status: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> tuple[list[TraceRecord], str | None]:
        """Query records with filters and cursor pagination.

        Returns (records, next_cursor). next_cursor is None when no more pages.
        """
        ...

    async def get(self, trace_id: str) -> TraceRecord | None:
        """Get a single record by trace_id."""
        ...

    async def verify_chain(self, start_seq: int = 0) -> bool:
        """Verify SHA-256 hash chain integrity from start_seq."""
        ...

    async def close(self) -> None:
        """Release resources."""
        ...


# ---------------------------------------------------------------------------
# JSONLTraceStore — append-only JSONL with hash chain and daily rotation
# ---------------------------------------------------------------------------


class JSONLTraceStore:
    """Append-only JSONL store with SHA-256 hash chain and daily rotation.

    File layout:
        {workspace}/traces/traces-{YYYY-MM-DD}.jsonl

    Hash chain:
        Each record's record_hash = SHA-256(JCS(record_without_hash) + prev_hash).
        First record of a file uses prev_hash from prior file's last record
        (or "0"*64 for genesis).

    Daily rotation:
        Last record of a day is a tombstone with event_type="rotation"
        pointing to the next file, carrying the chain hash forward.
    """

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self._traces_dir = workspace / "traces"
        self._traces_dir.mkdir(parents=True, exist_ok=True)
        # NIST AU-9: Protect audit information — owner-only access on traces dir
        self._traces_dir.chmod(0o700)
        self._lock = asyncio.Lock()
        self._last_hash: str = "0" * 64
        self._current_date: str = ""
        self._current_file: Path | None = None
        self._line_count: int = 0
        self._warm_started = False

    def _file_for_date(self, date_str: str) -> Path:
        """Return the JSONL file path for a given date."""
        return self._traces_dir / f"traces-{date_str}.jsonl"

    def _today(self) -> str:
        """Return today's date as YYYY-MM-DD."""
        return datetime.now(UTC).strftime("%Y-%m-%d")

    async def _warm_start(self) -> None:
        """Read the last hash from the most recent JSONL file.

        Also verifies the tail of the hash chain to detect tampering
        (NIST AU-10: Non-repudiation).
        """
        if self._warm_started:
            return

        files = sorted(self._traces_dir.glob("traces-*.jsonl"), reverse=True)
        if files:
            last_file = files[0]
            self._current_date = last_file.stem.replace("traces-", "")
            self._current_file = last_file
            # Read last line to get chain state
            lines = last_file.read_text().strip().split("\n")
            self._line_count = len(lines)
            if lines and lines[-1]:
                try:
                    last_record = json.loads(lines[-1])
                    self._last_hash = last_record.get("record_hash", "0" * 64)
                except json.JSONDecodeError:
                    logger.warning("Failed to parse last line of %s", last_file)

            # Verify tail of chain (last 10 records) for tamper detection
            self._verify_tail(lines)

        self._warm_started = True

    def _verify_tail(self, lines: list[str], tail_size: int = 10) -> None:
        """Verify hash chain linkage on the last N records.

        Logs a warning if tampering is detected. Does not block startup —
        full verification can be triggered via verify_chain().
        """
        valid_lines = [ln for ln in lines if ln.strip()]
        check_lines = valid_lines[-tail_size:] if len(valid_lines) > tail_size else valid_lines

        prev_hash: str | None = None
        for line in check_lines:
            try:
                data = json.loads(line)
                record = TraceRecord(**data)
            except (json.JSONDecodeError, Exception):
                logger.warning("Unparseable record in tail verification")
                return

            if prev_hash is not None and record.prev_hash != prev_hash:
                logger.error(
                    "TAMPER DETECTED: hash chain broken — expected prev=%s, got prev=%s",
                    prev_hash, record.prev_hash,
                )
                return

            expected = record.compute_hash()
            if record.record_hash != expected:
                logger.error(
                    "TAMPER DETECTED: record hash mismatch — stored=%s, computed=%s",
                    record.record_hash, expected,
                )
                return

            prev_hash = record.record_hash

    async def _maybe_rotate(self) -> None:
        """Rotate to a new file if the date has changed."""
        today = self._today()
        if today == self._current_date:
            return

        # Write rotation tombstone to current file
        if self._current_file is not None and self._current_file.exists():
            tombstone = TraceRecord(
                provider="system",
                model="system",
                event_type="rotation",
                event_data={"next_file": f"traces-{today}.jsonl"},
            ).with_hash(self._last_hash)
            self._last_hash = tombstone.record_hash
            with self._current_file.open("a") as f:
                f.write(json.dumps(tombstone.model_dump()) + "\n")

        self._current_date = today
        self._current_file = self._file_for_date(today)
        self._line_count = 0

    async def append(self, record: TraceRecord) -> None:
        """Append a record with hash chain linkage."""
        async with self._lock:
            await self._warm_start()
            await self._maybe_rotate()

            hashed = record.with_hash(self._last_hash)
            self._last_hash = hashed.record_hash
            self._line_count += 1

            if self._current_file is None:  # pragma: no cover — set by _maybe_rotate
                msg = "current_file not set after rotation"
                raise RuntimeError(msg)
            with self._current_file.open("a") as f:
                f.write(json.dumps(hashed.model_dump()) + "\n")
            # NIST AU-9: Owner read/write only on audit log files
            self._current_file.chmod(0o600)

    async def query(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        provider: str | None = None,
        agent: str | None = None,
        status: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> tuple[list[TraceRecord], str | None]:
        """Query records with filters. Reads newest-first."""
        async with self._lock:
            await self._warm_start()

        results: list[TraceRecord] = []

        # Determine starting point from cursor
        cursor_date: str | None = None
        cursor_line: int = -1
        if cursor:
            parts = cursor.split(":")
            if len(parts) == 2:
                cursor_date = parts[0]
                cursor_line = int(parts[1])

        # Iterate files newest-first
        files = sorted(self._traces_dir.glob("traces-*.jsonl"), reverse=True)
        for file_path in files:
            file_date = file_path.stem.replace("traces-", "")

            # Skip files before cursor
            if cursor_date and file_date > cursor_date:
                continue

            lines = file_path.read_text().strip().split("\n")

            # Determine starting line within file
            start_line = len(lines) - 1
            if cursor_date == file_date and cursor_line >= 0:
                start_line = cursor_line - 1

            for i in range(start_line, -1, -1):
                if not lines[i]:
                    continue
                try:
                    data = json.loads(lines[i])
                except json.JSONDecodeError:
                    continue

                rec = TraceRecord(**data)

                # Skip rotation tombstones
                if rec.event_type == "rotation":
                    continue

                # Apply filters
                if provider and rec.provider != provider:
                    continue
                if agent and rec.agent_label != agent:
                    continue
                if status and rec.status != status:
                    continue
                if start and rec.timestamp < start:
                    continue
                if end and rec.timestamp > end:
                    continue

                results.append(rec)

                if len(results) >= limit:
                    next_cursor = f"{file_date}:{i}"
                    return results, next_cursor

        return results, None

    async def get(self, trace_id: str) -> TraceRecord | None:
        """Get a single record by trace_id. Scans newest files first."""
        files = sorted(self._traces_dir.glob("traces-*.jsonl"), reverse=True)
        for file_path in files:
            for line in file_path.read_text().strip().split("\n"):
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("trace_id") == trace_id:
                    return TraceRecord(**data)
        return None

    async def verify_chain(self, start_seq: int = 0) -> bool:
        """Verify hash chain integrity across all JSONL files."""
        files = sorted(self._traces_dir.glob("traces-*.jsonl"))
        prev_hash = "0" * 64
        seq = 0

        for file_path in files:
            for line in file_path.read_text().strip().split("\n"):
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    return False

                if seq < start_seq:
                    prev_hash = data.get("record_hash", "0" * 64)
                    seq += 1
                    continue

                record = TraceRecord(**data)

                # Verify prev_hash linkage
                if record.prev_hash != prev_hash:
                    logger.error(
                        "Hash chain break at seq %d: expected prev=%s, got prev=%s",
                        seq, prev_hash, record.prev_hash,
                    )
                    return False

                # Verify self-hash
                expected = record.compute_hash()
                if record.record_hash != expected:
                    logger.error(
                        "Hash mismatch at seq %d: stored=%s, computed=%s",
                        seq, record.record_hash, expected,
                    )
                    return False

                prev_hash = record.record_hash
                seq += 1

        return True

    async def close(self) -> None:
        """No resources to release for file-based store."""
