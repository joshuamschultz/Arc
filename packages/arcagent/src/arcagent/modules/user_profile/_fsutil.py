"""Filesystem helpers shared across the user_profile module."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def atomic_write(path: Path, text: str) -> None:
    """Write *text* to *path* atomically using a temp file + os.replace().

    If the process is killed between the write() and replace() calls the
    original file at *path* is left intact; only the temp file is orphaned.
    Temp files are written to the same directory as *path* so that
    os.replace() is guaranteed to be within the same filesystem. Permissions
    are locked to 0o600 (owner read/write only) before the rename.
    """
    dir_ = path.parent
    dir_.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix=".tmp", prefix=path.stem + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp_path, path)
    except Exception:  # reason: re-raise after cleaning up the temp file
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


__all__ = ["atomic_write"]
