"""Per-agent registry of delivery channels the agent has been reached on.

A raw ``platform:chat_id`` only exists on an inbound event — pairings and
sessions persist only hashed identifiers (privacy by design), so nothing else
can tell an operator *which* channels a schedule could deliver to. The turn path
records each interactive channel it handles here so arcui can present a friendly
dropdown ("Telegram — Josh") instead of asking a non-technical user to type a
``platform:chat_id`` string.

This is AGENT STATE: written with direct filesystem I/O to the agent's
workspace (never via the LLM's file tools), same as sessions/memory (ADR-029).
Newest-first, deduped by target, capped — a small recency list, not a log.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arcagent.utils.io import atomic_write_text

_logger = logging.getLogger("arcagent.known_channels")

_FILE = "channels.json"

# Recency cap — a picker source, not history. Old channels fall off the end.
MAX_CHANNELS = 20


def _path(workspace: Path) -> Path:
    return workspace / _FILE


def _read(workspace: Path) -> list[dict[str, Any]]:
    path = _path(workspace)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def record(workspace: Path, *, target: str, label: str) -> None:
    """Upsert ``target`` (with its friendly ``label``) as a known channel.

    A re-seen target moves to the front and refreshes its label. Blank targets
    are ignored. Fail-open: a write error is logged, never raised — recording a
    channel must not fail a turn.
    """
    if not target:
        return
    try:
        entries = [e for e in _read(workspace) if e.get("target") != target]
        entry = {
            "target": target,
            "label": label or target,
            "last_seen": datetime.now(UTC).isoformat(),
        }
        entries.insert(0, entry)
        del entries[MAX_CHANNELS:]
        atomic_write_text(_path(workspace), json.dumps(entries, indent=2))
    except OSError:  # reason: fail-open — channel recording must not fail a turn
        _logger.warning("could not record known channel %s", target, exc_info=True)


def list_channels(workspace: Path) -> list[dict[str, str]]:
    """Return known channels as ``[{"target", "label"}]``, newest first."""
    return [
        {"target": str(e["target"]), "label": str(e.get("label") or e["target"])}
        for e in _read(workspace)
        if e.get("target")
    ]


__all__ = ["MAX_CHANNELS", "list_channels", "record"]
