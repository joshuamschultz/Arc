"""Always-on local spool recorder (SPEC-026 FR-2).

Server-independent, dependency-light (stdlib + pydantic — no DB driver). Every
arcllm/arcrun/arcagent action appends one durable JSONL line the moment it
happens, whether or not any store, server, DB, or UI is running. A later-started
store layer backfills from this file.

Design (SDD §4 + Research §11.2):
- ``os.write`` of one newline-terminated record = a single syscall, atomic for
  concurrent appenders on local filesystems (the inode mutex serializes them).
  ``file.write`` is avoided because CPython can split large writes into 4 KB
  chunks, breaking that atomicity. No ``fcntl`` lock is needed — the spool keeps
  no in-memory state (unlike the arctrust WORM chain).
- No per-record ``fsync``: the contract is "survives process crash", not "OS
  crash". Hard durability lives in the arctrust WORM, not here.
- Fail-open (AU-5 / NFR-3): a write error is logged and swallowed — telemetry
  must never break the call it is recording.
- Daily rotation (``operational-YYYY-MM-DD.jsonl``) gives the ingester a clean
  per-file inode/offset model.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from arcstore.config import resolve_data_dir
from arcstore.records import SpoolRecord

_logger = logging.getLogger("arcstore.spool")

_FILE_MODE = 0o600
"""Owner-only — the spool may carry sensitive metadata (NFR-5)."""

# Task-local run correlation id. arcrun binds this around a run so every record
# emitted inside it (llm_call, tool_event, run_event) shares one ``request_id``
# without each producer having to thread the run id through its call stack. A
# ContextVar (not a global) keeps concurrent runs isolated — ``asyncio.Task``
# snapshots the context at creation, so spawned children carry their own copy.
_request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "arcstore_request_id", default=None
)


@contextlib.contextmanager
def request_context(request_id: str) -> Iterator[None]:
    """Bind the active run correlation id for spool records on this task.

    Records appended within the ``with`` block that do not already carry a
    ``request_id`` inherit ``request_id``. An explicit id on a record always
    wins. The binding is restored on exit, so it never leaks across runs.
    """
    token = _request_id_var.set(request_id)
    try:
        yield
    finally:
        _request_id_var.reset(token)


def spool_path(*, data_dir: Path | None = None) -> Path:
    """Default spool file for today under the resolved Arc data dir."""
    base = data_dir if data_dir is not None else resolve_data_dir()
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    return base / "spool" / f"operational-{day}.jsonl"


def record(rec: SpoolRecord, *, path: Path | None = None) -> None:
    """Append one record to the spool. Always-on, fail-open (AU-5).

    Args:
        rec: The record to persist.
        path: Override the destination file (defaults to today's spool).
    """
    try:
        if rec.request_id is None:
            active = _request_id_var.get()
            if active is not None:
                rec = rec.model_copy(update={"request_id": active})
        target = path if path is not None else spool_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec.model_dump(mode="json"), ensure_ascii=True) + "\n"
        fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, _FILE_MODE)
        try:
            os.fchmod(fd, _FILE_MODE)  # tighten perms regardless of umask / pre-existing file
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:  # reason: fail-open — telemetry must never break the audited call (AU-5)
        _logger.warning("arcstore.spool.record failed — swallowing (AU-5)", exc_info=True)


def read_complete_segments(path: Path, offset: int) -> tuple[list[bytes], int]:
    """Read all complete (newline-terminated) byte segments from ``offset``; leave torn tail.

    Shared torn-tail byte-cursor algorithm used by both the spool and WORM readers.
    Returns ``(chunks, new_offset)`` — the caller parses chunks in its own format.
    """
    if not path.exists():
        return [], offset
    with path.open("rb") as fh:
        fh.seek(offset)
        data = fh.read()
    if not data:
        return [], offset
    segments = data.split(b"\n")
    # The final element is either b"" (newline-terminated) or a torn partial line;
    # in both cases it is excluded from the complete set.
    complete = [s for s in segments[:-1] if s]
    torn_len = len(segments[-1])
    return complete, offset + len(data) - torn_len


def read_from_offset(path: Path, offset: int) -> tuple[list[SpoolRecord], int]:
    """Read complete records from ``offset`` to EOF; never consume a torn tail.

    Returns ``(records, new_offset)`` where ``new_offset`` advances only past
    fully-newline-terminated lines, so the store can persist it as a resumable
    byte cursor. Corrupt complete lines are skipped (operational telemetry).
    """
    chunks, new_offset = read_complete_segments(path, offset)
    records: list[SpoolRecord] = []
    for chunk in chunks:
        try:
            records.append(SpoolRecord.model_validate_json(chunk))
        except Exception:  # reason: corrupt complete line — skip + log, never abort ingest
            _logger.warning("arcstore.spool.read_from_offset skipping corrupt line in %s", path)
    return records, new_offset


def read(path: Path) -> Iterator[SpoolRecord]:
    """Iterate records from a spool file, skipping corrupt/torn lines.

    A torn final line after a crash (or any malformed line) is logged and
    skipped — the stream never aborts. This is safe because the spool is
    operational telemetry; durability of the compliance record is the WORM's job.
    """
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                yield SpoolRecord.model_validate_json(stripped)
            except Exception:  # reason: torn/corrupt line — skip + log, never abort the stream
                _logger.warning("arcstore.spool.read skipping corrupt line %d in %s", lineno, path)
