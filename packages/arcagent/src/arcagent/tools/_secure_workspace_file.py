"""Descriptor-relative primitives for authorized workspace files.

Path policy is decided by ``resolve_workspace_path``. These helpers preserve
that decision through the actual I/O by walking from the authorized root with
``dir_fd`` operations and ``O_NOFOLLOW`` instead of reopening the resolved
pathname after validation.
"""

from __future__ import annotations

import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path


class SecureWorkspaceFileError(OSError):
    """A descriptor-relative file operation failed closed."""


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _select_root(path: Path, roots: tuple[Path, ...]) -> tuple[Path, tuple[str, ...]]:
    matches = [root.resolve() for root in roots if _within(path, root.resolve())]
    if not matches:
        raise SecureWorkspaceFileError("authorized path has no authorized root")
    root = max(matches, key=lambda candidate: len(candidate.parts))
    return root, path.relative_to(root).parts


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _open_parent(
    path: Path,
    roots: tuple[Path, ...],
    *,
    create: bool,
) -> tuple[int, str, Path]:
    root, parts = _select_root(path, roots)
    if not parts:
        raise SecureWorkspaceFileError("authorized root is not a file")
    fd = os.open(root, _directory_flags())
    try:
        for part in parts[:-1]:
            if part in {"", ".", ".."}:
                raise SecureWorkspaceFileError("invalid path component")
            try:
                child = os.open(part, _directory_flags(), dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, mode=0o755, dir_fd=fd)
                child = os.open(part, _directory_flags(), dir_fd=fd)
            os.close(fd)
            fd = child
        return fd, parts[-1], root
    except BaseException:
        os.close(fd)
        raise


def _identity(info: os.stat_result) -> FileIdentity:
    return FileIdentity(device=info.st_dev, inode=info.st_ino)


def _verify_parent_identity(path: Path, parent_fd: int) -> None:
    """Fail if the lexical parent no longer names the opened directory."""
    opened = _identity(os.fstat(parent_fd))
    current = os.stat(path.parent, follow_symlinks=False)
    if not stat.S_ISDIR(current.st_mode) or _identity(current) != opened:
        raise SecureWorkspaceFileError("parent directory changed during file operation")


def read_regular_file(
    path: Path,
    roots: tuple[Path, ...],
    *,
    max_bytes: int,
) -> tuple[bytes, FileIdentity]:
    """Read one bounded regular file through a no-follow descriptor."""
    parent_fd, name, _root = _open_parent(path, roots, create=False)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise SecureWorkspaceFileError("target is not a regular file")
            if info.st_size > max_bytes:
                raise SecureWorkspaceFileError(f"target exceeds {max_bytes} bytes")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(fd, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > max_bytes:
                raise SecureWorkspaceFileError(f"target exceeds {max_bytes} bytes")
            return data, _identity(info)
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def atomic_write_regular_file(
    path: Path,
    roots: tuple[Path, ...],
    data: bytes,
    *,
    expected: FileIdentity | None = None,
) -> None:
    """Atomically replace a regular file within its descriptor-opened parent."""
    parent_fd, name, _root = _open_parent(path, roots, create=True)
    temp_name = f".{name}.arc-{uuid.uuid4().hex}.tmp"
    temp_fd = -1
    try:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if current is not None and not stat.S_ISREG(current.st_mode):
            raise SecureWorkspaceFileError("target is not a regular file")
        if expected is not None and (current is None or _identity(current) != expected):
            raise SecureWorkspaceFileError("target changed while it was being edited")

        replacement_mode = stat.S_IMODE(current.st_mode) if current is not None else 0o666
        temp_fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            replacement_mode,
            dir_fd=parent_fd,
        )
        view = memoryview(data)
        while view:
            written = os.write(temp_fd, view)
            view = view[written:]
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = -1

        # Recheck edit identity immediately before the same-directory replace.
        if expected is not None:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if _identity(current) != expected:
                raise SecureWorkspaceFileError("target changed while it was being edited")
        _verify_parent_identity(path, parent_fd)
        os.replace(temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        try:
            os.unlink(temp_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


__all__ = [
    "FileIdentity",
    "SecureWorkspaceFileError",
    "atomic_write_regular_file",
    "read_regular_file",
]
