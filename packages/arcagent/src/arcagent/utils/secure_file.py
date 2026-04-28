"""Secure file write/read helpers — single source for SR-1 semantics.

Every component that handles a credential on disk (the ArcUI agent
token, future vault cache, federated peer keys) needs the same
guarantees:

  * 0600 from creation (no umask race window)
  * `O_NOFOLLOW` on read (rejects symlink swaps)
  * `fstat` on the same fd that produced the bytes (closes TOCTOU)
  * 0700 parent directory (prevents symlink-attack on the file)

Centralizing the recipe means a future vault bridge or replicated key
store can just call `write_secret` / `read_secret_owned` and inherit
the federal-compliance posture instead of reinventing it. Wave 2 review
TD-MED.
"""

from __future__ import annotations

import os
from pathlib import Path


def write_secret(path: Path, contents: bytes | str) -> None:
    """Atomically write `contents` to `path` with 0600 perms from creation.

    Behavior:
      * Parent directory is created (mkdir parents=True) and tightened
        to 0o700 — best-effort; existing dirs aren't loosened.
      * File is opened with `O_CREAT|O_TRUNC|O_WRONLY` mode 0o600. On
        first creation the kernel applies 0o600 directly (no umask
        race). On overwrite, `fchmod` tightens after open (existing
        perms are NOT inherited from the kernel).
      * Bytes/str are written, fd is closed.

    No atomic replace via tempfile + rename today; existing callers are
    single-process single-writer (one process owns one path). If a
    multi-writer scenario emerges, switch to temp+rename here.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        parent.chmod(0o700)
    except OSError:
        # Best-effort tightening; if the parent existed with other
        # contents, we still want the file write to succeed and carry
        # its own 0o600 mode bit.
        pass

    data = contents.encode("utf-8") if isinstance(contents, str) else contents
    fd = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        # Wave 1 C-1 fix: `os.open(..., 0o600)` only applies on file
        # *creation*. On overwrite the inode keeps its old perms (which
        # may be world-readable from a prior write_text). fchmod
        # tightens to 0o600 every time, atomically, on the fd we own.
        os.fchmod(fd, 0o600)
        os.write(fd, data)
    finally:
        os.close(fd)


def read_secret_owned(path: Path) -> tuple[bytes | None, str]:
    """Read `path` only if it is 0600 and owned by the current UID.

    Returns (contents, "ok") on success; (None, "<reason>") on any
    failure mode. The reasons map to SPEC-019 SR-1 audit categories so
    callers can forward them to `ui.agent_autoconnect`:
      - "absent": file does not exist
      - "open_failed_<errno>": OS refused (loop, permission, etc.)
      - "stat_failed": fstat raised
      - "wrong_owner": owner != current UID
      - "loose_perms": any group/world bits set
      - "read_failed": data read raised

    The single-fd pattern (open + fstat + read on the SAME fd) closes
    the TOCTOU window between perm-check and read (Wave 1 H-3 fix).
    `O_NOFOLLOW` rejects symlink swaps performed between caller's
    `path.exists()` and our open.
    """
    if not path.exists():
        return None, "absent"

    flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        fd = os.open(str(path), flags)
    except OSError as exc:
        return None, f"open_failed_{exc.errno}"

    try:
        try:
            st = os.fstat(fd)
        except OSError:
            return None, "stat_failed"
        if st.st_uid != os.getuid():
            return None, "wrong_owner"
        if st.st_mode & 0o077:
            return None, "loose_perms"
        try:
            return os.read(fd, 4096), "ok"
        except OSError:
            return None, "read_failed"
    finally:
        os.close(fd)
