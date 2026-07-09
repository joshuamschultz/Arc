"""Shared path validation for workspace-scoped tools.

All file-based tools (read, write, edit) must validate that
resolved paths remain within the agent's workspace boundary.
This prevents directory traversal attacks (e.g., ../../etc/passwd).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from arcagent.core.errors import ToolError

_logger = logging.getLogger("arcagent.tools.validation")

# Goal-bearing / control-plane files that are operator-authored and read-only
# to the agent's own mutating tools at every tier (SPEC-035 REQ-001/002, ASI01).
DEFAULT_PROTECTED_NAMES: tuple[str, ...] = ("identity.md", "policy.md", "context.md")

ProtectedAuditSink = Callable[[str, dict[str, Any]], None]


def resolve_protected_paths(workspace: Path, extra: list[str]) -> frozenset[Path]:
    """Resolve the operator-declared protected-path set once, for the session.

    Unions the built-in defaults (``identity.md``/``policy.md``/``context.md``)
    with the operator's ``tools.policy.protected_paths`` entries. Relative
    entries resolve against the workspace root; absolute entries are honored
    as-is. The returned frozenset is immutable for the session (REQ-002).
    """
    ws = workspace.resolve()
    paths: set[Path] = {(ws / name).resolve() for name in DEFAULT_PROTECTED_NAMES}
    for entry in extra:
        candidate = Path(entry)
        resolved = candidate.resolve() if candidate.is_absolute() else (ws / candidate).resolve()
        paths.add(resolved)
    return frozenset(paths)


def _inode_key(path: Path) -> tuple[int, int] | None:
    """Return ``(st_dev, st_ino)`` for an existing path, else None.

    Inode identity is the ground truth for "same file": it is invariant across
    name case (case-insensitive APFS/NTFS), symlinks, and hardlinks — where a
    resolved-path STRING comparison silently disagrees. ``resolve()`` on macOS
    does not canonicalize case, so ``IDENTITY.md`` and ``identity.md`` stringify
    differently while sharing one inode.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _casenorm(path: Path) -> str:
    """Case- and separator-normalized path string for pre-creation comparison."""
    return os.path.normcase(str(path)).casefold()


def is_protected_path(resolved: Path, protected: frozenset[Path]) -> bool:
    """True iff a resolved path is in the operator-protected (read-only) set.

    Comparison is by INODE IDENTITY when the target exists — defeating the
    case-fold (``IDENTITY.md``), symlink, and hardlink aliases a resolved-path
    string check misses on case-insensitive filesystems. For a target that does
    not exist yet (a to-be-created file), it falls back to a case-normalized
    path comparison so creating ``IDENTITY.md`` where ``identity.md`` is
    protected is still denied.
    """
    target = resolved.resolve()
    target_key = _inode_key(target)
    target_norm = _casenorm(target)
    for candidate in protected:
        cand = candidate.resolve()
        if target_key is not None:
            if target_key == _inode_key(cand):
                return True
        elif target_norm == _casenorm(cand):
            return True
    return False


def enforce_protected_path(
    resolved: Path,
    protected: frozenset[Path],
    *,
    tool_name: str,
    file_path: str,
    caller_did: str = "did:arc:unknown",
    audit_sink: ProtectedAuditSink | None = None,
) -> None:
    """Deny + audit a mutation of a protected path (REQ-001/004).

    Raises :class:`ToolError` (``TOOL_PROTECTED_PATH``) and emits a
    ``tool.protected_path.denied`` audit event carrying the tool name, caller
    DID, and target path. No-op when the path is not protected.
    """
    if not is_protected_path(resolved, protected):
        return
    if audit_sink is not None:
        try:
            audit_sink(
                "tool.protected_path.denied",
                {
                    "tool": tool_name,
                    "actor_did": caller_did,
                    "path": str(resolved),
                    "reason": "protected path is read-only to the agent (SPEC-035 goal-lock)",
                },
            )
        except Exception:  # reason: fail-open — audit must not mask the denial
            _logger.exception("Protected-path audit sink raised; continuing")
    raise ToolError(
        code="TOOL_PROTECTED_PATH",
        message=(
            f"'{file_path}' is a protected, operator-authored path and is read-only to the agent"
        ),
        details={"path": str(resolved), "tool": tool_name},
    )


# Redirection / in-place-write targets a shell command may mutate. Best-effort
# only (OQ-2): a host shell can evade naive parsing via $(...) / eval / here-docs.
# Real enforcement at enterprise/federal comes from the sandbox read-only mount
# (REQ-023). At personal this catches the obvious `echo x > identity.md`.
_REDIRECT_RE = re.compile(r">>?\s*([^\s;|&<>]+)")
_INPLACE_RE = re.compile(r"\b(?:tee|dd\s+of=|truncate\s+-s\s*\d+)\s+([^\s;|&<>]+)")


def scan_shell_for_protected_writes(
    command: str, workspace: Path, protected: frozenset[Path]
) -> Path | None:
    """Return the first protected path an obvious shell write would hit, else None.

    Best-effort host-bash guard (OQ-2, advisory at personal tier). Matches
    ``>``/``>>`` redirections and a few in-place writers (``tee``, ``dd of=``,
    ``truncate``); resolves each target against the workspace and checks the
    protected set. Not a security boundary — the sandbox mount is (REQ-023).
    """
    ws = workspace.resolve()
    for match in [*_REDIRECT_RE.finditer(command), *_INPLACE_RE.finditer(command)]:
        raw = match.group(1).strip().strip("'\"")
        if not raw:
            continue
        candidate = Path(raw)
        resolved = candidate.resolve() if candidate.is_absolute() else (ws / candidate).resolve()
        if is_protected_path(resolved, protected):
            return resolved
    return None


def _is_within(path: Path, boundary: Path) -> bool:
    """Check if *path* is inside *boundary* without raising."""
    try:
        path.relative_to(boundary)
        return True
    except ValueError:
        return False


def resolve_workspace_path(
    file_path: str,
    workspace: Path,
    *,
    allow_symlinks: bool = False,
    allowed_paths: list[Path] | None = None,
) -> Path:
    """Resolve a file path within the workspace boundary.

    Accepts absolute or relative paths. Relative paths are resolved
    against the workspace root. Absolute paths must fall within the
    workspace directory or one of the allowed_paths.

    Args:
        file_path: The path string to resolve.
        workspace: The workspace root directory.
        allow_symlinks: If False (default), reject symlinks.
        allowed_paths: Additional directories that are permitted
            beyond the workspace boundary. Used by tools.policy config.

    Raises:
        ToolError: If the path contains null bytes, is a symlink (when
                   disallowed), or escapes all permitted boundaries.
    """
    # Null byte injection guard
    if "\x00" in file_path:
        raise ToolError(
            code="TOOL_INVALID_PATH",
            message="Path contains null bytes",
            details={"path": repr(file_path)},
        )

    workspace = workspace.resolve()
    candidate = Path(file_path)

    unresolved = candidate if candidate.is_absolute() else workspace / candidate

    # Symlink check: walk path components within workspace boundary.
    # Paths outside workspace are skipped (boundary check rejects them).
    if not allow_symlinks and _is_within(unresolved, workspace):
        check = workspace
        for part in unresolved.relative_to(workspace).parts:
            check = check / part
            if check.is_symlink():
                raise ToolError(
                    code="TOOL_SYMLINK_DENIED",
                    message=f"Symlinks are not allowed: {file_path}",
                    details={"path": file_path, "symlink_at": str(check)},
                )

    resolved = unresolved.resolve()

    # Check workspace boundary, then allowed_paths
    if _is_within(resolved, workspace):
        return resolved

    if allowed_paths:
        for allowed in allowed_paths:
            if _is_within(resolved, allowed.resolve()):
                return resolved

    raise ToolError(
        code="TOOL_PATH_OUTSIDE_WORKSPACE",
        message=f"Path '{file_path}' resolves outside workspace",
        details={"path": file_path, "workspace": str(workspace)},
    )
