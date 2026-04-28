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
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import jcs  # type: ignore[import-untyped]  # RFC 8785 canonical JSON — no stubs available
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# Chunk size for the streaming reverse-line reader. 64KB matches typical
# Linux fread buffer and keeps memory bounded regardless of file size.
_REVERSE_READ_CHUNK = 64 * 1024


def _read_lines_reverse(
    path: Path, before_line_idx: int | None = None
) -> Iterator[tuple[int, str]]:
    """Yield (line_index, line_str) pairs from a file in reverse order.

    Wave 2 perf fix for `JSONLTraceStore.query`. The previous
    implementation did `read_text().split("\\n")`, materializing the
    entire line list before iterating — for a 10K-line file with
    `limit=50`, the query allocated 10K str objects to use 50.

    This version scans the file once for newline byte-positions
    (storing only ints, ~8 bytes per line) then seeks + reads each line
    on demand. Memory cost: O(line count × 8 bytes) instead of
    O(file size × per-line allocation overhead). Caller can stop
    iteration early; remaining lines are never decoded.

    `before_line_idx` lets pagination resume from a known position
    (cursor format `"<date>:<line_idx>"`); the first yielded pair is at
    `before_line_idx - 1`. Pass `None` to start from the last line.

    Line indices are 0-based from start of file. A trailing newline is
    treated as terminating the previous line, not as starting an empty
    one — matches the existing JSONL convention.
    """
    if not path.exists():
        return
    size = path.stat().st_size
    if size == 0:
        return

    with path.open("rb") as fh:
        # First pass: collect newline byte-offsets without materializing
        # any line content. This is O(size) scanned but O(line count)
        # memory — for a 10K-line × 1KB file, ~80KB of ints vs ~10MB of
        # decoded strings.
        newline_positions: list[int] = []
        offset = 0
        while True:
            chunk = fh.read(_REVERSE_READ_CHUNK)
            if not chunk:
                break
            base = offset
            start = 0
            while True:
                idx = chunk.find(b"\n", start)
                if idx == -1:
                    break
                newline_positions.append(base + idx)
                start = idx + 1
            offset += len(chunk)

        # Total lines: if file ends with \n the last newline terminates
        # the last line. If not, there's an extra partial line trailing.
        ends_with_newline = bool(newline_positions) and newline_positions[-1] == size - 1
        total_lines = (
            len(newline_positions)
            if ends_with_newline
            else len(newline_positions) + 1
        )
        if total_lines == 0:
            return

        last_idx = total_lines - 1
        start_idx = (
            min(before_line_idx, last_idx + 1) - 1
            if before_line_idx is not None
            else last_idx
        )
        if start_idx < 0:
            return

        # Walk lines in reverse. line[i] occupies bytes
        # [prev_newline + 1, this_newline_or_eof].
        for i in range(start_idx, -1, -1):
            line_start = (
                newline_positions[i - 1] + 1 if i > 0 else 0
            )
            line_end = (
                newline_positions[i]
                if i < len(newline_positions)
                else size
            )
            fh.seek(line_start)
            line_bytes = fh.read(line_end - line_start)
            yield i, line_bytes.decode("utf-8", errors="replace")


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

    def iter_records(self) -> AsyncIterator[dict[str, Any]]:
        """Stream all records as plain dicts in chronological storage order.

        SPEC-019 T2.1: required for multi-store warm start and federated
        queries that need full history. Memory bounded per record (no full
        file load). Skips rotation tombstones and unparseable lines.
        """
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
        {agent_root}/traces/traces-{YYYY-MM-DD}.jsonl
        (where agent_root = workspace.parent if workspace name is "workspace",
         else workspace itself — traces live OUTSIDE the agent's workspace
         sandbox per NIST AU-9)

    Hash chain:
        Each record's record_hash = SHA-256(JCS(record_without_hash) + prev_hash).
        First record of a file uses prev_hash from prior file's last record
        (or "0"*64 for genesis).

    Daily rotation:
        Last record of a day is a tombstone with event_type="rotation"
        pointing to the next file, carrying the chain hash forward.
    """

    def __init__(self, agent_root: Path) -> None:
        # NIST AU-9: traces live at <agent_root>/traces, outside the agent's
        # workspace tool sandbox.
        self._traces_dir = agent_root / "traces"
        self._traces_dir.mkdir(parents=True, exist_ok=True)
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
        """Query records with filters. Reads newest-first.

        Wave 2 perf fix: lines are streamed from end-of-file via a
        reverse chunked reader instead of `read_text().split("\\n")`.
        For a 10K-line file with limit=50, the previous implementation
        read all 10K lines into memory; this version stops after parsing
        ~50 records. Federation amplifies the saving by K stores.
        """
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

            cursor_line_for_file = (
                cursor_line - 1 if cursor_date == file_date else None
            )
            for line_idx, line in await asyncio.to_thread(
                _read_lines_reverse, file_path, cursor_line_for_file
            ):
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                rec = TraceRecord(**data)

                if rec.event_type == "rotation":
                    continue
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
                    next_cursor = f"{file_date}:{line_idx}"
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

    async def iter_records(self) -> AsyncIterator[dict[str, Any]]:
        """Yield every record as a dict, oldest day first.

        SPEC-019 T2.2: streams line-by-line — never holds a full file in
        memory. Skips blank lines, malformed JSON (logged), and rotation
        tombstones. File reads are dispatched to a thread to avoid blocking
        the event loop on large files.
        """
        files = sorted(self._traces_dir.glob("traces-*.jsonl"))
        for file_path in files:
            async for record in self._iter_file(file_path):
                yield record

    async def _iter_file(self, file_path: Path) -> AsyncIterator[dict[str, Any]]:
        """Yield records from a single JSONL file, line by line."""
        # Push the blocking read off the event loop. For large files this
        # also serves as a yield point (asyncio gets a chance to run).
        text = await asyncio.to_thread(file_path.read_text)
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Skipping malformed line in %s", file_path.name)
                continue
            if data.get("event_type") == "rotation":
                continue
            yield data

    async def close(self) -> None:
        """No resources to release for file-based store."""
