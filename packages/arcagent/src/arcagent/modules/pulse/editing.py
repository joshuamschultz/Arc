"""Operator writes to ``pulse.md``: add, edit, remove one check.

``pulse.md`` is protected from the agent's own tools, so this is the one path
that changes it. Callers (the audited arcui route and the CLI behind it) must
already have checked the operator role. Every write is validated against the
``pulse.md`` format and replaced atomically. A write never approves anything:
a new check has no approval slot, and an edited check keeps its old slot so
dispatch sees a digest mismatch and treats it as "changes pending" until the
control authority signs the new text.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from arcagent.modules.pulse.approval import APPROVED_FILE, PULSE_FILE, _atomic_write, _read_json
from arcagent.modules.pulse.engine import _SECTION_RE, parse_pulse_file

MAX_INTERVAL_MINUTES = 525_600  # one year
MAX_ACTION_CHARS = 4000
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
# A field marker inside the action text could plant a second slot (for example a
# fake approval) on the same line, so it is refused outright.
_MARKER_RE = re.compile(r"\*\*\s*(Approval|Interval|Action)\s*:\s*\*\*", re.IGNORECASE)
_APPROVAL_LINE_RE = re.compile(r"^-\s+\*\*Approval:\*\*.*$", re.IGNORECASE | re.MULTILINE)


class PulseCheckInvalidError(ValueError):
    """The requested check change is not valid pulse.md content."""


def validate_check(name: str, interval_minutes: int, action: str) -> str:
    """Validate a check and return its action flattened to the one-line stored form."""
    if not _NAME_RE.fullmatch(name):
        raise PulseCheckInvalidError(
            "name must be 1-64 letters, digits, '_', '-' or '.', starting with a letter or digit"
        )
    if isinstance(interval_minutes, bool) or not 1 <= interval_minutes <= MAX_INTERVAL_MINUTES:
        raise PulseCheckInvalidError(
            f"interval_minutes must be between 1 and {MAX_INTERVAL_MINUTES}"
        )
    flat = " ".join(action.split())
    if not flat:
        raise PulseCheckInvalidError("action text is required")
    if len(flat) > MAX_ACTION_CHARS:
        raise PulseCheckInvalidError(f"action text is longer than {MAX_ACTION_CHARS} characters")
    if _MARKER_RE.search(flat):
        raise PulseCheckInvalidError("action text may not contain pulse.md field markers")
    return flat


def _render(name: str, interval_minutes: int, action: str) -> str:
    return f"## {name}\n- **Interval:** {interval_minutes} minutes\n- **Action:** {action}\n"


def _read(workspace: Path) -> str:
    try:
        return (workspace / PULSE_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _write(workspace: Path, text: str) -> None:
    path = workspace / PULSE_FILE
    workspace.mkdir(parents=True, exist_ok=True)
    mode = os.stat(path).st_mode & 0o777 if path.exists() else 0o600
    _atomic_write(path, text, mode)


def _span(source: str, name: str) -> tuple[int, int] | None:
    """(start, end) of check ``name``'s section in ``source``; None if absent."""
    sections = list(_SECTION_RE.finditer(source))
    for index, match in enumerate(sections):
        if match.group(1) == name:
            end = sections[index + 1].start() if index + 1 < len(sections) else len(source)
            return match.start(), end
    return None


def _exists(source: str, name: str) -> bool:
    return any(check.name == name for check in parse_pulse_file(source))


def add_pulse_check(workspace: Path, *, name: str, interval_minutes: int, action: str) -> None:
    """Append a new, unapproved check to ``pulse.md`` (created if absent)."""
    flat = validate_check(name, interval_minutes, action)
    source = _read(workspace)
    if _span(source, name) is not None or _exists(source, name):
        raise PulseCheckInvalidError(f"pulse check '{name}' already exists")
    separator = (
        "" if not source or source.endswith("\n\n") else "\n" if source.endswith("\n") else "\n\n"
    )
    _write(workspace, source + separator + _render(name, interval_minutes, flat))


def edit_pulse_check(workspace: Path, name: str, *, interval_minutes: int, action: str) -> None:
    """Rewrite check ``name``; its stored approval slot stays and goes stale."""
    flat = validate_check(name, interval_minutes, action)
    source = _read(workspace)
    span = _span(source, name)
    if span is None:
        raise PulseCheckInvalidError(f"pulse check '{name}' not found")
    start, end = span
    old_approval = _APPROVAL_LINE_RE.search(source[start:end])
    block = _render(name, interval_minutes, flat)
    if old_approval is not None:
        head, tail = block.split("- **Action:**", 1)
        block = f"{head}{old_approval.group(0)}\n- **Action:**{tail}"
    trailing = "\n" if end < len(source) else ""
    _write(workspace, source[:start] + block + trailing + source[end:])


def remove_pulse_check(workspace: Path, name: str) -> None:
    """Delete check ``name`` and its review snapshot."""
    source = _read(workspace)
    span = _span(source, name)
    if span is None:
        raise PulseCheckInvalidError(f"pulse check '{name}' not found")
    start, end = span
    _write(workspace, source[:start] + source[end:])
    snapshots = _read_json(workspace / APPROVED_FILE)
    if name in snapshots:
        del snapshots[name]
        _atomic_write(workspace / APPROVED_FILE, json.dumps(snapshots, indent=2), 0o600)


__all__ = [
    "MAX_ACTION_CHARS",
    "MAX_INTERVAL_MINUTES",
    "PulseCheckInvalidError",
    "add_pulse_check",
    "edit_pulse_check",
    "remove_pulse_check",
    "validate_check",
]
