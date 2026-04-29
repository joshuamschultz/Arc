"""Session store — light wrapper over arcagent.core.session_manager.

Exposes a clean, module-level API (append, read, iter_messages) so the rest
of the session module has a single integration point with the core.  Does NOT
duplicate any logic from SessionManager — purely additive delegation.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

_logger = logging.getLogger("arcagent.modules.session.store")


def sessions_dir(workspace: Path) -> Path:
    """Return the path to the sessions directory for a workspace."""
    return workspace / "sessions"


def jsonl_path_for(workspace: Path, session_id: str) -> Path:
    """Return the JSONL file path for a given session ID."""
    return sessions_dir(workspace) / f"{session_id}.jsonl"


def iter_session_files(workspace: Path) -> list[Path]:
    """Return all JSONL session files in the workspace sessions directory.

    Sorted by modification time (oldest first) so the indexer processes
    files in a stable, predictable order.
    """
    sdir = sessions_dir(workspace)
    if not sdir.exists():
        return []
    return sorted(sdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)


def read_messages_from_offset(path: Path, start_offset: int) -> tuple[list[dict[str, Any]], int]:
    """Read complete JSONL lines starting from byte offset.

    Reads from *start_offset* to EOF, parses only complete lines (lines
    that end with '\\n'), and returns the parsed entries plus the new byte
    offset.  Partial lines at EOF are ignored — they will be picked up on
    the next poll once the writer finishes the line.  This is the core of
    the crash-safe, idempotent polling pattern.

    Returns (entries, new_offset).  On read error the function logs a
    warning and returns ([], start_offset) so the caller can retry later.
    """
    if not path.exists():
        return [], start_offset

    try:
        with open(path, "rb") as fh:
            fh.seek(start_offset)
            raw = fh.read()
            new_offset = start_offset + len(raw)
    except OSError:
        _logger.warning("Could not read %s at offset %d", path, start_offset)
        return [], start_offset

    entries: list[dict[str, Any]] = []
    # Only process bytes that form complete lines (ended with '\n').
    # Find the last '\n' — anything after it is an incomplete line.
    last_newline = raw.rfind(b"\n")
    if last_newline == -1:
        # No complete lines yet; return to last committed offset.
        return [], start_offset

    complete_bytes = raw[: last_newline + 1]
    # Adjust new_offset to only credit the bytes we actually indexed.
    new_offset = start_offset + last_newline + 1

    for raw_line in complete_bytes.split(b"\n"):
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line.decode("utf-8"))
            entries.append(entry)
        except (json.JSONDecodeError, UnicodeDecodeError):
            _logger.warning("Skipping malformed JSONL line in %s", path)

    return entries, new_offset
