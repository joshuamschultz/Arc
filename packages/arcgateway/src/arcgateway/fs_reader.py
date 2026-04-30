"""Read-only filesystem chokepoint for ``team/<agent>/`` access.

The single audited entry point for ALL filesystem reads from arcui (and any
other gateway consumer). No write surface exists — the module exposes only
:func:`read_file` and :func:`list_tree`. Adding write helpers here is forbidden
by structural test (:class:`TestReadOnlyByStructure`).

Security invariants
-------------------
* Path traversal is blocked at the single chokepoint via ``Path.resolve()`` +
  ``commonpath`` check. Symlinks are followed by ``resolve()``, so symlink
  escapes are caught.
* Maximum read size is :data:`MAX_READ_BYTES` (1 MiB). DoS protection.
* Tree listings are depth-capped (:data:`MAX_TREE_DEPTH`) and entry-capped
  (:data:`MAX_TREE_ENTRIES`).
* Hidden entries (names starting with ``.``) are excluded from tree listings.
* Every operation emits a NIST AU-2 audit event via :mod:`arcgateway.audit`.

Forward compatibility
---------------------
``scope`` accepts ``"agent" | "team" | "shared"`` from day one. Only
``"agent"`` is wired today; ``"team"`` and ``"shared"`` raise
:class:`NotImplementedError`. This lets us add team-shared-knowledge in a
future spec without API churn (D-002).
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from arcgateway.audit import emit_event

logger = logging.getLogger(__name__)

Scope = Literal["agent", "team", "shared"]

MAX_READ_BYTES: int = 1_048_576  # 1 MiB
MAX_TREE_DEPTH: int = 10
MAX_TREE_ENTRIES: int = 5_000

# Filename suffixes treated as plain text. Anything not in here AND not JSON
# falls through to base64 binary handling.
_TEXT_SUFFIXES: frozenset[str] = frozenset({
    ".md", ".txt", ".py", ".toml", ".jsonl", ".log", ".yaml", ".yml",
    ".html", ".css", ".js", ".ts", ".sh", ".rst", ".ini", ".cfg", "",
})
_JSON_SUFFIXES: frozenset[str] = frozenset({".json"})


class PathTraversalError(ValueError):
    """Raised when a relative path attempts to escape the scope root."""


class FileTooLargeError(ValueError):
    """Raised when a target file exceeds :data:`MAX_READ_BYTES`."""


@dataclass(frozen=True)
class FileEntry:
    """One entry in a tree listing."""

    path: str  # path relative to scope root, forward-slash separated
    type: Literal["file", "dir"]
    size: int  # bytes for files, 0 for dirs
    mtime: float  # POSIX timestamp


@dataclass(frozen=True)
class FileContent:
    """A read file's content + metadata.

    For ``content_type == "binary"``, ``content`` is base64-encoded.
    """

    path: str
    size: int
    mtime: float
    content: str
    content_type: Literal["text", "binary", "json"]


def read_file(
    *,
    scope: Scope,
    agent_id: str,
    agent_root: Path | None,
    rel_path: str,
    caller_did: str,
) -> FileContent:
    """Read a single file under the agent root.

    Args:
        scope: ``"agent"`` is the only implemented value; ``"team"`` and
            ``"shared"`` raise :class:`NotImplementedError`.
        agent_id: The agent's stable id, recorded in the audit event.
        agent_root: Resolved path to ``team/<agent_id>/``. Required for
            ``scope="agent"``.
        rel_path: Path relative to ``agent_root``. Forward slashes; no leading
            slash. Traversal attempts (``..``, absolute, symlink escape) raise
            :class:`PathTraversalError`.
        caller_did: DID of the caller; recorded in the audit event.

    Returns:
        :class:`FileContent` with the file's content + metadata.

    Raises:
        NotImplementedError: For ``scope`` other than ``"agent"``.
        PathTraversalError: Path escapes the agent root.
        FileNotFoundError: Target does not exist or is not a regular file.
        FileTooLargeError: Target exceeds :data:`MAX_READ_BYTES`.
    """
    root = _resolve_root(scope, agent_root)
    target = _validate_path(root, rel_path)

    emit_event(
        action="gateway.fs.read",
        target=f"{scope}:{agent_id}:{rel_path}",
        outcome="allow",
        extra={
            "scope": scope,
            "agent_id": agent_id,
            "path": rel_path,
            "caller_did": caller_did,
        },
    )

    if not target.exists() or not target.is_file():
        raise FileNotFoundError(rel_path)

    stat = target.stat()
    if stat.st_size > MAX_READ_BYTES:
        raise FileTooLargeError(f"{rel_path}: {stat.st_size} > {MAX_READ_BYTES}")

    suffix = target.suffix.lower()
    if suffix in _TEXT_SUFFIXES:
        return FileContent(
            path=rel_path,
            size=stat.st_size,
            mtime=stat.st_mtime,
            content=target.read_text(encoding="utf-8", errors="replace"),
            content_type="text",
        )
    if suffix in _JSON_SUFFIXES:
        return FileContent(
            path=rel_path,
            size=stat.st_size,
            mtime=stat.st_mtime,
            content=target.read_text(encoding="utf-8"),
            content_type="json",
        )
    return FileContent(
        path=rel_path,
        size=stat.st_size,
        mtime=stat.st_mtime,
        content=base64.b64encode(target.read_bytes()).decode("ascii"),
        content_type="binary",
    )


def list_tree(
    *,
    scope: Scope,
    agent_id: str,
    agent_root: Path | None,
    rel_path: str = "",
    max_depth: int = MAX_TREE_DEPTH,
    caller_did: str,
) -> list[FileEntry]:
    """List a directory subtree (depth- and entry-capped).

    Args:
        scope: ``"agent"`` only. ``"team"``/``"shared"`` raise
            :class:`NotImplementedError`.
        agent_id: Agent id for the audit event.
        agent_root: Resolved path to the agent root.
        rel_path: Path relative to ``agent_root``. Empty string lists from the
            root itself.
        max_depth: Maximum recursion depth. Default :data:`MAX_TREE_DEPTH`.
        caller_did: DID of the caller; audited.

    Returns:
        List of :class:`FileEntry` (capped at :data:`MAX_TREE_ENTRIES`),
        traversed in sorted order.

    Raises:
        NotImplementedError: For non-agent scopes.
        PathTraversalError: ``rel_path`` escapes ``agent_root``.
    """
    root = _resolve_root(scope, agent_root)
    base = _validate_path(root, rel_path) if rel_path else root

    emit_event(
        action="gateway.fs.tree",
        target=f"{scope}:{agent_id}:{rel_path}",
        outcome="allow",
        extra={
            "scope": scope,
            "agent_id": agent_id,
            "path": rel_path,
            "caller_did": caller_did,
        },
    )

    if not base.exists() or not base.is_dir():
        return []

    entries: list[FileEntry] = []
    for child in _walk(base, max_depth):
        if len(entries) >= MAX_TREE_ENTRIES:
            break
        rel = child.relative_to(root).as_posix()
        st = child.stat()
        entries.append(
            FileEntry(
                path=rel,
                type="dir" if child.is_dir() else "file",
                size=st.st_size if child.is_file() else 0,
                mtime=st.st_mtime,
            )
        )
    return entries


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _resolve_root(scope: Scope, agent_root: Path | None) -> Path:
    if scope == "agent":
        if agent_root is None:
            raise ValueError("scope='agent' requires agent_root")
        return agent_root.resolve()
    if scope in ("team", "shared"):
        raise NotImplementedError(
            f"scope={scope!r} is not implemented (forward-compat placeholder)"
        )
    raise ValueError(f"unknown scope: {scope!r}")


def _validate_path(root: Path, rel: str) -> Path:
    if rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
        # Reject absolute paths up front (POSIX or Windows-style).
        raise PathTraversalError(f"absolute path not allowed: {rel}")
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PathTraversalError(f"path escapes root: {rel}") from exc
    return candidate


def _walk(base: Path, max_depth: int, depth: int = 0) -> Iterator[Path]:
    if depth > max_depth:
        return
    try:
        children = sorted(base.iterdir())
    except OSError:
        return
    for child in children:
        if child.name.startswith("."):
            continue
        yield child
        if child.is_dir() and not child.is_symlink():
            yield from _walk(child, max_depth, depth + 1)
