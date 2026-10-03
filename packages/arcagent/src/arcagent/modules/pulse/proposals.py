"""Agent-filed pulse proposals.

An agent can never write ``pulse.md``. It may file a proposal here; a proposal
runs nothing and signs nothing. An operator reads it in the pulse panel, and
only if they add the check through the audited route does it become a real
(still unapproved) check.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arcagent.modules.pulse.approval import _atomic_write
from arcagent.modules.pulse.editing import PulseCheckInvalidError, validate_check

PROPOSALS_FILE = "pulse-proposals.json"
MAX_PROPOSALS = 20
MAX_REASON_CHARS = 500


def list_proposals(workspace: Path) -> list[dict[str, Any]]:
    """Pending proposals; a missing or corrupt file reads as none."""
    try:
        data = json.loads((workspace / PROPOSALS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict) and isinstance(item.get("name"), str)]


def _store(workspace: Path, proposals: list[dict[str, Any]]) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    _atomic_write(workspace / PROPOSALS_FILE, json.dumps(proposals, indent=2), 0o600)


def propose_pulse_check(
    workspace: Path, *, name: str, interval_minutes: int, action: str, reason: str
) -> None:
    """File (or replace) a pending proposal after validating it like a real check."""
    flat = validate_check(name, interval_minutes, action)
    others = [p for p in list_proposals(workspace) if p["name"] != name]
    if len(others) >= MAX_PROPOSALS:
        raise PulseCheckInvalidError(f"too many pending proposals (max {MAX_PROPOSALS})")
    others.append(
        {
            "name": name,
            "interval_minutes": interval_minutes,
            "action": flat,
            "reason": " ".join(reason.split())[:MAX_REASON_CHARS],
        }
    )
    _store(workspace, others)


def clear_proposal(workspace: Path, name: str) -> None:
    """Drop the pending proposal ``name`` (accepted or dismissed by the operator)."""
    remaining = [p for p in list_proposals(workspace) if p["name"] != name]
    _store(workspace, remaining)


__all__ = [
    "MAX_PROPOSALS",
    "PROPOSALS_FILE",
    "clear_proposal",
    "list_proposals",
    "propose_pulse_check",
]
