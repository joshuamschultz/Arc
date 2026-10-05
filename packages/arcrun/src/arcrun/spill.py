"""Restorable tool-output spill (SPEC-070, agent-loop research G7).

A tool result larger than the run's spill threshold is not cut. The full text is
written to the run's own spill directory and the model sees its head plus a
handle it can use to read the rest in bounded chunks or search it. Nothing is
dropped, so a large read never loses the part the task needed.

Layering: arcrun owns tool execution, so it owns the one place a result gets too
large for the context window. The *location* is the caller's: arcrun never
invents a path, it spills under the ``work_dir`` the host hands the run (ADR-029:
direct filesystem I/O into the agent's own workspace, never an LLM-facing file
tool). With no ``work_dir`` there is nowhere to spill, so the result passes
through whole, since a lossy cut is not an alternative.

Scope: a handle is an unguessable capability minted by one run's store. The store
resolves only handles it issued itself, so a handle from another run or agent
(whose files live in a different directory) is refused, never read.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arcrun.types import Tool, ToolContext

_logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = 4
# Tokens the model keeps of a spilled result, ahead of the marker.
HEAD_TOKENS = 2000
# Single-call ceiling for read_tool_output: a backstop far above normal use. A
# larger request is clamped and the remainder is reached with the next offset,
# so nothing is lost even here.
MAX_READ_TOKENS = 200_000
DEFAULT_READ_TOKENS = 8000
MAX_SEARCH_MATCHES = 20
SEARCH_CONTEXT_CHARS = 200

READ_TOOL_NAME = "read_tool_output"
SEARCH_TOOL_NAME = "search_tool_output"
SPILL_TOOLS = frozenset({READ_TOOL_NAME, SEARCH_TOOL_NAME})

_RUN_DIR_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SPILL_SUBDIR = "spill"


def estimate_tokens(text: str) -> int:
    """Token estimate used for every spill threshold, offset and limit."""
    return -(-len(text) // CHARS_PER_TOKEN)


def spill_root(work_dir: Path) -> Path:
    """The directory holding every run's spill directory under ``work_dir``."""
    return work_dir / _SPILL_SUBDIR


@dataclass(frozen=True)
class SpillRecord:
    """What a spill returns: the handle and the size of what was saved."""

    handle: str
    tokens: int
    length: int
    digest: str


class SpillStore:
    """One run's spill directory plus the handles it has issued."""

    def __init__(self, work_dir: Path, run_id: str) -> None:
        self.run_id = run_id
        # A pinned run id is caller text; only a plain name may become a path part.
        name = (
            run_id if _RUN_DIR_RE.fullmatch(run_id) else hashlib.sha256(run_id.encode()).hexdigest()
        )
        self._dir = spill_root(work_dir) / name
        self._issued: dict[str, SpillRecord] = {}

    @property
    def directory(self) -> Path:
        return self._dir

    def spill(self, text: str) -> SpillRecord:
        """Write ``text`` whole under a fresh opaque handle and return its record."""
        raw = text.encode("utf-8")
        handle = f"spill_{secrets.token_hex(16)}"
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp_name = tempfile.mkstemp(dir=self._dir, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle_file:
                handle_file.write(raw)
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self._dir / handle)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        record = SpillRecord(
            handle=handle,
            tokens=estimate_tokens(text),
            length=len(text),
            digest=hashlib.sha256(raw).hexdigest(),
        )
        self._issued[handle] = record
        return record

    def load(self, handle: str) -> str | None:
        """The full text behind ``handle``, or ``None`` when it is not this run's.

        Refuses any handle this store never issued (which covers malformed and
        traversing text), a file that is a symlink, and a file whose content no
        longer matches the digest recorded at write time (tampering in the
        workspace between write and read).
        """
        record = self._issued.get(handle)
        if record is None:
            return None
        path = self._dir / handle
        if path.is_symlink() or not path.is_file():
            return None
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != record.digest:
            return None
        return raw.decode("utf-8")


def head_with_marker(text: str, record: SpillRecord) -> str:
    """What the model sees for a spilled result: its head and how to get the rest."""
    head = text[: HEAD_TOKENS * CHARS_PER_TOKEN]
    marker = (
        f"Full output is {record.tokens} tokens and was saved as {record.handle}. "
        f"Read more with {READ_TOOL_NAME}(handle, offset, limit) or search it "
        f"with {SEARCH_TOOL_NAME}(handle, query)."
    )
    return f"{head}\n\n{marker}"


def _refuse(ctx: ToolContext, reason: str) -> str:
    """Audit and answer a refused handle without echoing what the caller sent."""
    if ctx.event_bus is not None:
        ctx.event_bus.emit(
            "tool_output.refused",
            {"tool_call_id": ctx.tool_call_id, "turn_number": ctx.turn_number, "reason": reason},
        )
    return "Error: no saved tool output for that handle in this run."


def _wrong_run(store: SpillStore, ctx: ToolContext) -> bool:
    return ctx.run_id != store.run_id


def _read_tool(store: SpillStore) -> Tool:
    async def execute(params: dict[str, Any], ctx: ToolContext) -> str:
        if _wrong_run(store, ctx):
            return _refuse(ctx, "run_mismatch")
        text = store.load(params["handle"])
        if text is None:
            return _refuse(ctx, "unknown_handle")
        total = estimate_tokens(text)
        offset = int(params.get("offset", 0))
        limit = min(int(params.get("limit", DEFAULT_READ_TOKENS)), MAX_READ_TOKENS)
        end = min(offset + limit, total)
        chunk = text[offset * CHARS_PER_TOKEN : end * CHARS_PER_TOKEN]
        more = f"Next offset: {end}." if end < total else "End of output."
        return f"{chunk}\n[{READ_TOOL_NAME}: tokens {offset}-{end} of {total}. {more}]"

    return Tool(
        name=READ_TOOL_NAME,
        description=(
            "Read part of a tool result that was too large to show whole and was "
            "saved. offset and limit count tokens (about 4 characters each). "
            f"Limit defaults to {DEFAULT_READ_TOKENS} and is capped at "
            f"{MAX_READ_TOKENS}. Continue from the Next offset the reply gives."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "handle": {"type": "string", "description": "Handle from the saved-output note."},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "default": DEFAULT_READ_TOKENS},
            },
            "required": ["handle"],
            "additionalProperties": False,
        },
        execute=execute,
        classification="read_only",
    )


def _search_tool(store: SpillStore) -> Tool:
    async def execute(params: dict[str, Any], ctx: ToolContext) -> str:
        if _wrong_run(store, ctx):
            return _refuse(ctx, "run_mismatch")
        text = store.load(params["handle"])
        if text is None:
            return _refuse(ctx, "unknown_handle")
        query = str(params["query"])
        haystack, needle = text.lower(), query.lower()
        hits: list[str] = []
        start = haystack.find(needle)
        while start != -1 and len(hits) < MAX_SEARCH_MATCHES:
            lo = max(0, start - SEARCH_CONTEXT_CHARS)
            snippet = text[lo : start + len(query) + SEARCH_CONTEXT_CHARS]
            hits.append(f"@token {start // CHARS_PER_TOKEN}: {snippet}")
            start = haystack.find(needle, start + len(needle))
        if not hits:
            return f"[{SEARCH_TOOL_NAME}: no match for the query]"
        capped = " (first matches only)" if start != -1 else ""
        body = "\n---\n".join(hits)
        note = f"{len(hits)} match(es){capped}; read around one with {READ_TOOL_NAME}"
        return f"[{SEARCH_TOOL_NAME}: {note}]\n{body}"

    return Tool(
        name=SEARCH_TOOL_NAME,
        description=(
            "Find text inside a saved tool result (case-insensitive plain text, "
            "not a pattern). Each match gives the token offset to read around."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "handle": {"type": "string", "description": "Handle from the saved-output note."},
                "query": {"type": "string", "minLength": 1},
            },
            "required": ["handle", "query"],
            "additionalProperties": False,
        },
        execute=execute,
        classification="read_only",
    )


def spill_tools(store: SpillStore) -> list[Tool]:
    """The two read-only tools bound to ``store`` — one run's handles and no other."""
    return [_read_tool(store), _search_tool(store)]


def prune_spills(work_dir: Path, keep_runs: int) -> int:
    """Delete all but the ``keep_runs`` newest run spill directories; return removed count.

    Same newest-N rule as session retention, so a spill does not outlive the
    sessions that could point at it.
    """
    root = spill_root(work_dir)
    if not root.is_dir():
        return 0
    runs = sorted(
        (p for p in root.iterdir() if p.is_dir() and not p.is_symlink()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    stale = runs[keep_runs:]
    for path in stale:
        shutil.rmtree(path, ignore_errors=True)
        _logger.info("Removed old tool-output spill: %s", path.name)
    return len(stale)
