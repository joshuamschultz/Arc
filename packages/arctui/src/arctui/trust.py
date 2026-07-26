"""Folder-trust for arctui (SPEC-058 COMP-016, REQ-142).

Launching arctui in a project folder must not silently give the agent access to
it. The operator confirms "is this folder safe?"; on confirmation the folder is
added to the agent's ``tools.policy.allowed_paths`` **in memory, before startup**
— a session-scoped grant that is never written back to ``arcagent.toml``. The
agent's fixed workspace and ``protected_paths`` denials remain in force (arc's
``resolve_workspace_path`` treats allowed_paths as *additional* roots, with
protected_paths still overlay-denying).

This module is the pure grant core; the launch confirmation prompt lives in
``entry.py`` (a thin I/O layer over :func:`grant_folder`).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arcagent.core.config import ArcAgentConfig


def _allowed(config: ArcAgentConfig) -> list[str]:
    paths: list[str] = config.tools.policy.allowed_paths
    return paths


def folder_needs_trust(config: ArcAgentConfig, folder: Path) -> bool:
    """True when ``folder`` is not already an allowed path (so it needs a grant)."""
    target = str(folder.resolve())
    return target not in {str(Path(p).resolve()) for p in _allowed(config)}


def grant_folder(config: ArcAgentConfig, folder: Path) -> None:
    """Grant the agent read/write in ``folder`` for this session (in-memory only).

    Appends the resolved folder to ``tools.policy.allowed_paths`` (idempotent).
    Never writes ``arcagent.toml`` — the grant lives only in the loaded config
    object and vanishes when the process exits.
    """
    if folder_needs_trust(config, folder):
        config.tools.policy.allowed_paths = [*_allowed(config), str(folder.resolve())]


__all__ = ["folder_needs_trust", "grant_folder"]
