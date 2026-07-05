"""Trace query helpers — newest-first reads over JSONL trace files.

Companion to `arcllm.trace_store`. Persistence (write path, hash chain,
rotation) lives there; this module owns the read path: filtered query
with cursor pagination, single-record lookup, and the streaming
reverse-line reader that backs both.

Free functions take a `traces_dir: Path` so they can be reused by
federation (multi-store fan-out) without binding to a specific store
instance.
"""

import asyncio
import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arcllm._trace_crypto import unseal
from arcllm.exceptions import ArcLLMConfigError, ArcLLMTraceNotFoundError
from arcllm.trace_store import TraceRecord
from arcllm.types import Message, Tool

logger = logging.getLogger(__name__)


# Chunk size for the streaming reverse-line reader. 64KB matches typical
# Linux fread buffer and keeps memory bounded regardless of file size.
_REVERSE_READ_CHUNK = 64 * 1024


def _read_lines_reverse(
    path: Path, before_line_idx: int | None = None
) -> Iterator[tuple[int, str]]:
    """Yield (line_index, line_str) pairs from a file in reverse order.

    Wave 2 perf fix for the trace query path. The previous
    implementation did `read_text().split("\\n")`, materializing the
    entire line list before iterating — for a 10K-line file with
    `limit=50`, the query allocated 10K str objects to use 50.

    This version scans the file once for newline byte-positions
    (storing only ints, ~8 bytes per line) then seeks + reads each line
    on demand. Memory cost: O(line count x 8 bytes) instead of
    O(file size x per-line allocation overhead). Caller can stop
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
        # memory — for a 10K-line x 1KB file, ~80KB of ints vs ~10MB of
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
        total_lines = len(newline_positions) if ends_with_newline else len(newline_positions) + 1
        if total_lines == 0:
            return

        last_idx = total_lines - 1
        start_idx = (
            min(before_line_idx, last_idx + 1) - 1 if before_line_idx is not None else last_idx
        )
        if start_idx < 0:
            return

        # Walk lines in reverse. line[i] occupies bytes
        # [prev_newline + 1, this_newline_or_eof].
        for i in range(start_idx, -1, -1):
            line_start = newline_positions[i - 1] + 1 if i > 0 else 0
            line_end = newline_positions[i] if i < len(newline_positions) else size
            fh.seek(line_start)
            line_bytes = fh.read(line_end - line_start)
            yield i, line_bytes.decode("utf-8", errors="replace")


def _matches_filters(
    rec: TraceRecord,
    *,
    provider: str | None,
    agent: str | None,
    status: str | None,
    start: str | None,
    end: str | None,
) -> bool:
    """Return True if `rec` passes every supplied filter."""
    if provider and rec.provider != provider:
        return False
    if agent:
        # agent_label format: "<agent_name>" OR "<agent_name>/<sub>"
        # (e.g. "scap_isso" or "scap_isso/memory" when emitted by a
        # sub-component of the agent). Prefix-match so /api/traces
        # ?agent=scap_isso returns both forms — exact match would
        # silently filter out every sub-component trace.
        label = rec.agent_label or ""
        if label != agent and not label.startswith(agent + "/"):
            return False
    if status and rec.status != status:
        return False
    if start and rec.timestamp < start:
        return False
    if end and rec.timestamp > end:
        return False
    return True


async def query_records(
    traces_dir: Path,
    *,
    limit: int = 50,
    cursor: str | None = None,
    provider: str | None = None,
    agent: str | None = None,
    status: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> tuple[list[TraceRecord], str | None]:
    """Read traces newest-first from JSONL files, applying filters.

    Wave 2 perf fix: lines are streamed from end-of-file via a reverse
    chunked reader instead of `read_text().split("\\n")`. For a 10K-line
    file with limit=50, the previous implementation read all 10K lines
    into memory; this version stops after parsing ~50 records.

    Returns (records, next_cursor). next_cursor is None when no more
    pages.
    """
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
    files = sorted(traces_dir.glob("traces-*.jsonl"), reverse=True)
    for file_path in files:
        file_date = file_path.stem.replace("traces-", "")

        # Skip files before cursor
        if cursor_date and file_date > cursor_date:
            continue

        cursor_line_for_file = cursor_line - 1 if cursor_date == file_date else None
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
            if not _matches_filters(
                rec,
                provider=provider,
                agent=agent,
                status=status,
                start=start,
                end=end,
            ):
                continue

            results.append(rec)

            if len(results) >= limit:
                next_cursor = f"{file_date}:{line_idx}"
                return results, next_cursor

    return results, None


async def get_record(traces_dir: Path, trace_id: str) -> TraceRecord | None:
    """Return the record with `trace_id`, scanning newest files first."""
    files = sorted(traces_dir.glob("traces-*.jsonl"), reverse=True)
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


# ---------------------------------------------------------------------------
# Replay reconstruction (SPEC-016) — data-only. Replay EXECUTION (re-invoking
# a model, diffing/scoring outputs) and lineage CONSTRUCTION belong to
# arcrun/arcagent, never here (D-442, D-443; see SDD Boundaries).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayRequest:
    """A byte-exact, reconstructed LLM request. Data only — no I/O.

    Deliberately exposes no ``execute``/``invoke`` method: re-running the
    request, streaming the re-run, and diffing/scoring old vs. new output
    are arcrun/tooling responsibilities (D-442). ``classification`` rides
    the object verbatim from the source record so a caller applies the
    same access controls to a decrypted replay as it would to the
    original (a replay is a CUI read, not a downgrade to unmarked data).
    """

    provider: str
    model: str
    messages: list[Message]
    tools: list[Tool] | None
    options: dict[str, Any]
    lineage: dict[str, Any] | None
    classification: str


def _rebuild_request_from_body(
    request_body: dict[str, Any],
) -> tuple[list[Message], list[Tool] | None, dict[str, Any]]:
    """Reconstruct (messages, tools, options) from a stored request_body."""
    messages = [Message(**m) for m in request_body.get("messages", [])]
    tools_data = request_body.get("tools")
    tools = [Tool(**t) for t in tools_data] if tools_data else None
    options = {k: v for k, v in request_body.items() if k not in ("messages", "tools")}
    return messages, tools, options


async def load_for_replay(
    traces_dir: Path,
    trace_id: str,
    *,
    wrapping_key_resolver: Callable[[str], bytes] | None = None,
) -> ReplayRequest:
    """Reconstruct a byte-exact :class:`ReplayRequest` for ``trace_id``.

    Transparently decrypts sealed bodies when the record carries an
    ``encryption`` envelope. Reconstruction only — arcllm never re-invokes
    a model (see module docstring).

    Args:
        traces_dir: Directory containing ``traces-*.jsonl`` files.
        trace_id: The record to reconstruct.
        wrapping_key_resolver: Given an ``EncryptedEnvelope.key_ref``,
            returns the KEK bytes that wrapped that record's data key.
            Required when the record is encrypted; supports KEK rotation
            since each record resolves its OWN stored key_ref.

    Raises:
        ArcLLMTraceNotFoundError: No record matches ``trace_id``.
        ArcLLMConfigError: The record is encrypted but no
            ``wrapping_key_resolver`` was supplied, or the record was
            captured metadata-only (no body to reconstruct from) — fails
            closed rather than returning a partially-empty request.
    """
    record = await get_record(traces_dir, trace_id)
    if record is None:
        raise ArcLLMTraceNotFoundError(trace_id)

    if record.encryption is not None:
        if wrapping_key_resolver is None:
            raise ArcLLMConfigError(
                f"trace '{trace_id}' is encrypted; wrapping_key_resolver required"
            )
        wrapping_key = wrapping_key_resolver(record.encryption.key_ref)
        bodies = unseal(
            record.encryption,
            trace_id=record.trace_id,
            timestamp=record.timestamp,
            wrapping_key=wrapping_key,
        )
        request_body = bodies.get("request_body")
    else:
        request_body = record.request_body

    if request_body is None:
        raise ArcLLMConfigError(
            f"trace '{trace_id}' is not reconstructable (captured metadata-only; "
            "no request_body was stored)"
        )

    messages, tools, options = _rebuild_request_from_body(request_body)

    return ReplayRequest(
        provider=record.provider,
        model=record.model,
        messages=messages,
        tools=tools,
        options=options,
        lineage=record.lineage,
        classification=record.classification,
    )
