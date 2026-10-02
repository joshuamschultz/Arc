"""Read one regular file exactly once, never through a symlink leaf.

Every OKF reader verifies the bytes it read and then uses those same bytes. A
reader that checked a path and then opened it again (or followed a symlink the
attacker swapped in between) would verify one file and serve another. This is
the single open used for indexes, sidecars, logs and listed documents.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
# O_NONBLOCK keeps a planted FIFO from hanging the open; it has no effect on a
# regular file's reads.
_FLAGS |= getattr(os, "O_NONBLOCK", 0)


class UnsafeFileError(OSError):
    """The path is a symlink, not a regular file, or not the file that was expected."""


def read_regular_file(path: Path, *, expect: os.stat_result | None = None) -> bytes:
    """The bytes of ``path``, opened once with ``O_NOFOLLOW`` and checked with ``fstat``.

    Raises :class:`UnsafeFileError` for a symlink leaf, anything that is not a
    regular file, or (with ``expect``) a file whose device and inode differ from
    the one the caller listed. Other I/O failures raise ``OSError`` as usual.
    """
    try:
        fd = os.open(path, _FLAGS)
    except OSError as exc:
        if Path(path).is_symlink():
            raise UnsafeFileError(f"refusing symlink: {path}") from exc
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise UnsafeFileError(f"not a regular file: {path}")
        if expect is not None and (st.st_dev, st.st_ino) != (expect.st_dev, expect.st_ino):
            raise UnsafeFileError(f"file changed between listing and open: {path}")
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1 << 20):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


__all__ = ["UnsafeFileError", "read_regular_file"]
