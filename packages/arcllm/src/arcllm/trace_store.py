"""TraceStore — append-only, hash-chained LLM call recording.

TraceRecord captures everything about a single LLM invoke():
timing, tokens, cost, request/response bodies, and phase sub-timings.

Records are hash-chained (SHA-256): each record's hash covers the prior
record's hash, so in-place mutation or reordering of records present on
disk is detectable. This is INTERNAL-CONSISTENCY tamper evidence only —
it does not by itself prove no records were removed from the head of the
chain (see ``verify_chain()``); full AU-9/AU-10 non-repudiation requires
an external signed anchor (arctrust's ``WormSink``). ``JSONLTraceStore``
stays dependency-free of arctrust: wire a ``checkpoint_sink`` callback at
construction and this module builds+emits a pre-purge checkpoint (see
``arcllm.trace_retention.build_checkpoint``) at every rotation boundary;
the caller supplies the signer (e.g. arctrust's ``WormSink`` via
``emit()``). ``arcllm.trace_retention.verify_against_anchor`` is the
matching read-side check that a live store still contains an anchored
head. JSONLTraceStore implements the TraceStore Protocol with daily
rotation.

The read path (filtered query, single-record lookup, reverse-line
streaming) lives in `arcllm.trace_query`. This module owns schema +
persistence.
"""

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import jcs  # type: ignore[import-untyped]  # RFC 8785 canonical JSON — no stubs available
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TraceRecord — frozen Pydantic model for a single LLM call
# ---------------------------------------------------------------------------


class EncryptedEnvelope(BaseModel, frozen=True):
    """Envelope-encrypted trace bodies at rest (SPEC-016 D-438).

    Present iff ``request_body``/``response_body`` were sealed before
    write — in that case both are ``None`` on the record and the plaintext
    bodies only ever existed transiently in memory. ``ciphertext`` is the
    JCS-canonicalized ``{"request_body":..., "response_body":...}`` pair
    under AES-256-GCM; ``wrapped_key`` is the per-record data key wrapped
    with AES Key Wrap (RFC 3394 / SP 800-38F) under the resolved KEK.
    ``aad`` binds the ciphertext to this record's identity so it cannot be
    transplanted onto another record (D-448).
    """

    alg: str = "AES-256-GCM"
    wrapped_key: str
    key_ref: str
    nonce: str
    ciphertext: str
    aad: str


class TraceRecord(BaseModel, frozen=True):
    """Single LLM call record. Immutable, hashable, serializable."""

    trace_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    provider: str
    model: str
    agent_label: str | None = None
    agent_did: str | None = None
    budget_scope: str | None = None

    # Request body — full capture by default (SPEC-016 D-435). ``None``
    # when capture is disabled, the call errored before a request body
    # could be built, or the body was sealed into ``encryption`` below.
    request_body: dict[str, Any] | None = None

    # Response body — see request_body. ``None`` on the error path, when
    # capture is disabled, or when sealed into ``encryption``.
    response_body: dict[str, Any] | None = None

    # Classification watermark (SPEC-016 D-439). Config supplies the tier
    # FLOOR; a per-call value may be supplied but must never resolve below
    # that floor (arcllm enforces the floor, never classifies content).
    classification: str = "unclassified"

    # Envelope encryption (SPEC-016 D-438). Present iff request_body/
    # response_body were sealed before write.
    encryption: EncryptedEnvelope | None = None

    # Lineage token (SPEC-016 D-443), persisted VERBATIM from a
    # load_model()/invoke() kwarg. arcrun/arcagent build it; arcllm never
    # constructs or infers lineage from message content.
    lineage: dict[str, Any] | None = None

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
    event_type: Literal["llm_call", "config_change", "circuit_change", "rotation"] = "llm_call"
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

    def __init__(
        self,
        agent_root: Path,
        *,
        retention_max_age_days: int | None = None,
        retention_max_bytes: int | None = None,
        checkpoint_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
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
        # SPEC-016 D-440: retention purge runs at daily rotation boundaries,
        # never mid-day, so it never competes with today's live append.
        self._retention_max_age_days = retention_max_age_days
        self._retention_max_bytes = retention_max_bytes
        # Trace-checkpoint signed anchor: the caller supplies the signer
        # (e.g. arctrust's WormSink via emit()); this module only builds
        # and hands off the checkpoint. No arctrust import here — arcllm
        # stays dependency-free of arctrust (CLAUDE.md "don't mix concerns").
        self._checkpoint_sink = checkpoint_sink

    def _file_for_date(self, date_str: str) -> Path:
        """Return the JSONL file path for a given date."""
        return self._traces_dir / f"traces-{date_str}.jsonl"

    def _today(self) -> str:
        """Return today's date as YYYY-MM-DD."""
        return datetime.now(UTC).strftime("%Y-%m-%d")

    async def _warm_start(self) -> None:
        """Read the last hash from the most recent JSONL file.

        Also verifies the tail of the hash chain for internal-consistency
        tampering (in-place mutation or reordering of the last N records).
        This is NOT AU-10 non-repudiation on its own — it cannot detect a
        deleted head or a wholesale file swap; that requires the external
        signed anchor described on ``JSONLTraceStore``'s class docstring.
        """
        if self._warm_started:
            return

        files = sorted(self._traces_dir.glob("traces-*.jsonl"), reverse=True)
        if files:
            last_file = files[0]
            self._current_date = last_file.stem.replace("traces-", "")
            self._current_file = last_file
            # Read last line to get chain state. Dispatched to a worker
            # thread (M3) — this can be an arbitrarily large file and must
            # not block the event loop while the append() lock is held.
            text = await asyncio.to_thread(last_file.read_text)
            lines = text.strip().split("\n")
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
            except (json.JSONDecodeError, ValidationError, TypeError):
                logger.warning("Unparseable record in tail verification")
                continue

            if prev_hash is not None and record.prev_hash != prev_hash:
                logger.error(
                    "TAMPER DETECTED: hash chain broken — expected prev=%s, got prev=%s",
                    prev_hash,
                    record.prev_hash,
                )
                return

            expected = record.compute_hash()
            if record.record_hash != expected:
                logger.error(
                    "TAMPER DETECTED: record hash mismatch — stored=%s, computed=%s",
                    record.record_hash,
                    expected,
                )
                return

            prev_hash = record.record_hash

    async def _maybe_rotate(self) -> None:
        """Rotate to a new file if the date has changed."""
        today = self._today()
        if today == self._current_date:
            return

        # Write rotation tombstone to current file. Dispatched to a worker
        # thread (M3) — same blocking-write concern as append().
        if self._current_file is not None and self._current_file.exists():
            tombstone = TraceRecord(
                provider="system",
                model="system",
                event_type="rotation",
                event_data={"next_file": f"traces-{today}.jsonl"},
            ).with_hash(self._last_hash)
            self._last_hash = tombstone.record_hash
            await asyncio.to_thread(
                self._write_line_sync, self._current_file, tombstone.model_dump()
            )

        self._current_date = today
        self._current_file = self._file_for_date(today)
        self._line_count = 0

        await self._maybe_purge()

    async def _maybe_purge(self) -> None:
        """Anchor a pre-purge checkpoint, then run retention purge if configured.

        SPEC-016 D-440: purge only fires once per rotation (daily), never
        on every append, and only ever deletes already-rotated files — the
        file we just rotated *into* (today's) is never a purge candidate.
        Failures are logged, not raised: a purge hiccup must never break
        the calling append().

        The checkpoint anchor runs BEFORE purge deletes anything, and on
        every rotation regardless of whether retention is configured — the
        anchor's job is tamper detection (rollback past the last anchor),
        not retention bookkeeping, so it fires independently.
        """
        self._anchor_checkpoint()
        if self._retention_max_age_days is None and self._retention_max_bytes is None:
            return
        try:
            from arcllm.trace_retention import purge

            await purge(
                self._traces_dir,
                max_age_days=self._retention_max_age_days,
                max_bytes=self._retention_max_bytes,
            )
        except Exception:  # reason: a purge failure must never break append()
            logger.warning("Retention purge failed for %s", self._traces_dir, exc_info=True)

    def _anchor_checkpoint(self) -> None:
        """Build and emit a pre-purge checkpoint via ``checkpoint_sink``, if wired.

        Fail-open (NIST AU-5): a signer/sink failure must never break trace
        capture or the retention purge it precedes. No-op when no
        ``checkpoint_sink`` was supplied at construction.
        """
        if self._checkpoint_sink is None:
            return
        try:
            from arcllm.trace_retention import build_checkpoint

            checkpoint = build_checkpoint(self._traces_dir)
            self._checkpoint_sink(checkpoint)
        except Exception:  # reason: an anchor failure must never break capture
            logger.warning("Checkpoint anchor failed for %s", self._traces_dir, exc_info=True)

    @staticmethod
    def _write_line_sync(file_path: Path, payload: dict[str, Any]) -> None:
        """Append one JSON line to ``file_path``. Runs in a worker thread (M3)."""
        with file_path.open("a") as f:
            f.write(json.dumps(payload) + "\n")

    @staticmethod
    def _write_record_sync(record: TraceRecord, prev_hash: str, file_path: Path) -> TraceRecord:
        """Compute the hash-chain link and persist the record.

        Runs in a worker thread (M3) — hashing (JCS canonicalization +
        SHA-256), JSON serialization, and the file write are all
        synchronous CPU/IO work that must not block the event loop while
        the caller holds ``append()``'s chain-ordering lock.
        """
        hashed = record.with_hash(prev_hash)
        JSONLTraceStore._write_line_sync(file_path, hashed.model_dump())
        # NIST AU-9: Owner read/write only on audit log files
        file_path.chmod(0o600)
        return hashed

    async def append(self, record: TraceRecord) -> None:
        """Append a record with hash chain linkage.

        The lock serializes chain ordering; the actual hash/serialize/write
        work runs off the event loop via ``asyncio.to_thread`` (M3) so a
        single slow write can't stall every other coroutine awaiting this
        store while still preserving strict append ordering.
        """
        async with self._lock:
            await self._warm_start()
            await self._maybe_rotate()

            if self._current_file is None:  # pragma: no cover — set by _maybe_rotate
                msg = "current_file not set after rotation"
                raise RuntimeError(msg)

            hashed = await asyncio.to_thread(
                self._write_record_sync, record, self._last_hash, self._current_file
            )
            self._last_hash = hashed.record_hash
            self._line_count += 1

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

        Implementation lives in `arcllm.trace_query.query_records`.
        """
        # Lazy import: trace_query imports TraceRecord from this module.
        # Importing at top-level here would create a load-order cycle.
        from arcllm.trace_query import query_records

        async with self._lock:
            await self._warm_start()
        return await query_records(
            self._traces_dir,
            limit=limit,
            cursor=cursor,
            provider=provider,
            agent=agent,
            status=status,
            start=start,
            end=end,
        )

    async def get(self, trace_id: str) -> TraceRecord | None:
        """Get a single record by trace_id. Scans newest files first."""
        from arcllm.trace_query import get_record

        return await get_record(self._traces_dir, trace_id)

    async def verify_chain(self, start_seq: int = 0) -> bool:
        """Verify hash chain integrity across all JSONL files.

        The first record actually verified (seq == start_seq) is trusted
        for its own ``prev_hash`` rather than required to equal the
        hardcoded genesis ``"0"*64``. This is deliberate (SPEC-016 D-440,
        Research Insight): retention purge deletes whole rotated files
        oldest-first, so after a purge the oldest SURVIVING record
        legitimately points at a predecessor file that no longer exists.
        This function proves internal consistency among the records
        present on disk; it does NOT prove that no earlier records were
        removed — that requires an external signed anchor (see
        ``arcllm.trace_retention.build_checkpoint`` /
        ``verify_against_anchor``, and the ``checkpoint_sink`` wired at
        construction), a documented tamper-evidence limitation (AU-9/AU-10).
        """
        files = sorted(self._traces_dir.glob("traces-*.jsonl"))
        prev_hash: str | None = None
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

                if prev_hash is None:
                    prev_hash = record.prev_hash

                # Verify prev_hash linkage
                if record.prev_hash != prev_hash:
                    logger.error(
                        "Hash chain break at seq %d: expected prev=%s, got prev=%s",
                        seq,
                        prev_hash,
                        record.prev_hash,
                    )
                    return False

                # Verify self-hash
                expected = record.compute_hash()
                if record.record_hash != expected:
                    logger.error(
                        "Hash mismatch at seq %d: stored=%s, computed=%s",
                        seq,
                        record.record_hash,
                        expected,
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


__all__ = [
    "EncryptedEnvelope",
    "JSONLTraceStore",
    "TraceRecord",
    "TraceStore",
]
