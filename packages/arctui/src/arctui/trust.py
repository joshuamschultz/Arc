"""Folder-trust for ``arc tui`` launch — grant the served agent access to a project.

The coder keeps one isolated workspace (``[agent_root]/workspace``) reused across every
project. Launching ``arc tui`` in a new directory prompts to trust it and, on yes,
appends the resolved directory to the agent's ``[tools.policy].allowed_paths`` in its
``arcagent.toml``.

The grant is **persistent** (written to the toml), not in-memory: the agent runs in the
gateway process, not the TUI, so the TUI cannot mutate its live policy. Writing the toml
before the gateway serves the agent (the spawn path) is what makes the grant take effect;
an already-running gateway picks it up on its next restart. File tools resolve against
the workspace plus allowed_paths, so a trusted folder becomes readable AND writable.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

# Matches a single-line ``allowed_paths = [...]`` (arc agent create's format), capturing
# the ``allowed_paths = `` prefix so any inline comment after the ``]`` is preserved.
_ALLOWED_RE = re.compile(r"(?m)^(\s*allowed_paths\s*=\s*)\[.*?\]")


def _toml_str(value: str) -> str:
    """Render a string as a TOML basic string, escaping quotes/backslashes/control chars."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _allowed_paths(config_path: Path) -> list[str]:
    """Return the agent's current ``[tools.policy].allowed_paths`` (empty on any error)."""
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    policy = data.get("tools", {}).get("policy", {})
    paths = policy.get("allowed_paths", [])
    return [str(p) for p in paths] if isinstance(paths, list) else []


def folder_is_trusted(config_path: Path, folder: Path) -> bool:
    """True if ``folder`` is already granted (equal to, or under, an allowed path)."""
    target = folder.resolve()
    agent_dir = config_path.parent.resolve()
    for entry in _allowed_paths(config_path):
        base = Path(entry)
        base = (base if base.is_absolute() else agent_dir / base).resolve()
        if target == base or base in target.parents:
            return True
    return False


def grant_folder(config_path: Path, folder: Path) -> None:
    """Append ``folder`` (resolved, absolute) to the agent's allowed_paths. Idempotent.

    Preserves the file (and any inline comment) by rewriting only the ``allowed_paths``
    line; if the agent has no such line, a ``[tools.policy]`` block is appended.
    """
    resolved = str(folder.resolve())
    current = _allowed_paths(config_path)
    if resolved in current:
        return
    # Escape before interpolating into the toml — a folder name may contain a quote or
    # backslash, and this list is security policy the runtime trusts (SEC-01: no injection).
    rendered = "[" + ", ".join(_toml_str(p) for p in [*current, resolved]) + "]"
    text = config_path.read_text(encoding="utf-8")
    new_text, count = _ALLOWED_RE.subn(rf"\g<1>{rendered}", text, count=1)
    if count == 0:
        new_text = text.rstrip("\n") + f"\n\n[tools.policy]\nallowed_paths = {rendered}\n"
    config_path.write_text(new_text, encoding="utf-8")
