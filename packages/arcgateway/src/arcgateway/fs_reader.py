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
import errno
import logging
import mimetypes
import os
import stat
from collections.abc import Callable, Iterator
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
_TEXT_SUFFIXES: frozenset[str] = frozenset(
    {
        ".md",
        ".txt",
        ".py",
        ".toml",
        ".jsonl",
        ".log",
        ".yaml",
        ".yml",
        ".html",
        ".css",
        ".js",
        ".ts",
        ".sh",
        ".rst",
        ".ini",
        ".cfg",
        "",
    }
)
_JSON_SUFFIXES: frozenset[str] = frozenset({".json"})


class PathTraversalError(ValueError):
    """Raised when a relative path attempts to escape the scope root."""


class FileTooLargeError(ValueError):
    """Raised when a target file exceeds :data:`MAX_READ_BYTES`."""


class ReadAuthorizationError(PermissionError):
    """An opened file was refused before any bytes were read."""


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

    For ``content_type == "binary"``, ``content`` is base64-encoded and
    ``mime`` is what those bytes are — without it a caller holding the base64
    of a photo can do nothing with it but print it.
    """

    path: str
    size: int
    mtime: float
    content: str
    content_type: Literal["text", "binary", "json"]
    mime: str


def _mime_of(path: Path) -> str:
    """The media type of a file, never empty.

    ``application/octet-stream`` for anything unrecognised: a viewer must be
    able to branch on the answer without also handling ``None``.
    """
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def read_file(
    *,
    scope: Scope,
    agent_id: str,
    agent_root: Path | None,
    rel_path: str,
    caller_did: str,
    authorize_opened: Callable[[os.stat_result, os.stat_result], bool] | None = None,
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
        authorize_opened: Optional decision over pinned root/file metadata,
            called before file bytes are read.

    Returns:
        :class:`FileContent` with the file's content + metadata.

    Raises:
        NotImplementedError: For ``scope`` other than ``"agent"``.
        PathTraversalError: Path escapes the agent root.
        FileNotFoundError: Target does not exist or is not a regular file.
        FileTooLargeError: Target exceeds :data:`MAX_READ_BYTES`.
        ReadAuthorizationError: Opened root or file was not authorized.
    """
    root = _resolve_root(scope, agent_root)
    audit_target = f"{scope}:{agent_id}:{rel_path}"
    audit_extra = {"scope": scope, "agent_id": agent_id, "path": rel_path}
    try:
        target = _validate_path(root, rel_path)
        content_bytes, file_stat = _read_regular_nofollow(root, rel_path, authorize_opened)
    except (PathTraversalError, FileTooLargeError, FileNotFoundError, ReadAuthorizationError):
        emit_event(
            action="gateway.fs.read",
            target=audit_target,
            outcome="deny",
            actor_did=caller_did,
            extra=audit_extra,
        )
        raise
    except Exception:
        emit_event(
            action="gateway.fs.read",
            target=audit_target,
            outcome="error",
            actor_did=caller_did,
            extra=audit_extra,
        )
        raise
    emit_event(
        action="gateway.fs.read",
        target=audit_target,
        outcome="allow",
        actor_did=caller_did,
        extra=audit_extra,
    )

    suffix = target.suffix.lower()
    if suffix in _TEXT_SUFFIXES:
        return FileContent(
            path=rel_path,
            size=file_stat.st_size,
            mtime=file_stat.st_mtime,
            content=content_bytes.decode("utf-8", errors="replace"),
            content_type="text",
            mime=_mime_of(target),
        )
    if suffix in _JSON_SUFFIXES:
        return FileContent(
            path=rel_path,
            size=file_stat.st_size,
            mtime=file_stat.st_mtime,
            content=content_bytes.decode("utf-8"),
            content_type="json",
            mime=_mime_of(target),
        )
    return FileContent(
        path=rel_path,
        size=file_stat.st_size,
        mtime=file_stat.st_mtime,
        content=base64.b64encode(content_bytes).decode("ascii"),
        content_type="binary",
        mime=_mime_of(target),
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
        actor_did=caller_did,
        extra={
            "scope": scope,
            "agent_id": agent_id,
            "path": rel_path,
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
        return Path(agent_root).absolute()
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


def _read_regular_nofollow(
    root: Path,
    rel: str,
    authorize_opened: Callable[[os.stat_result, os.stat_result], bool] | None = None,
) -> tuple[bytes, os.stat_result]:
    """Read from one descriptor after opening every path component without symlinks."""
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise RuntimeError("secure file reads are unavailable on this platform")
    parts = Path(rel).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise PathTraversalError("unsafe relative path")
    flags = os.O_RDONLY | os.O_NOFOLLOW
    descriptors: list[int] = []
    try:
        absolute = root.absolute()
        descriptors.append(os.open(absolute.anchor, flags | os.O_DIRECTORY))
        for part in absolute.parts[1:]:
            descriptors.append(os.open(part, flags | os.O_DIRECTORY, dir_fd=descriptors[-1]))
        root_stat = os.fstat(descriptors[-1])
        for part in parts[:-1]:
            descriptors.append(os.open(part, flags | os.O_DIRECTORY, dir_fd=descriptors[-1]))
        descriptor = os.open(parts[-1], flags | os.O_NONBLOCK, dir_fd=descriptors[-1])
        descriptors.append(descriptor)
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            raise FileNotFoundError(rel)
        if authorize_opened is not None and authorize_opened(root_stat, file_stat) is not True:
            raise ReadAuthorizationError("opened file access denied")
        if file_stat.st_size > MAX_READ_BYTES:
            raise FileTooLargeError(f"{rel}: {file_stat.st_size} > {MAX_READ_BYTES}")
        chunks: list[bytes] = []
        remaining = MAX_READ_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > MAX_READ_BYTES:
            raise FileTooLargeError(f"{rel}: content exceeds {MAX_READ_BYTES}")
        return content, file_stat
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise PathTraversalError("symlinked file paths are unavailable") from exc
        if exc.errno == errno.ENOENT:
            raise FileNotFoundError(rel) from exc
        raise
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


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
