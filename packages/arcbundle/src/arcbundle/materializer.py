"""Atomic write of a verified bundle, and the inverse remove.

Nothing is written where it will finally live. The payload is staged into a
temporary sibling *inside* the destination root — same filesystem, so the
publishing ``os.replace`` is a single atomic rename — hardened to its final
modes, and only then swapped into place. A failure anywhere before that rename
leaves the destination byte-identical to its prior state, and the staging tree
is discarded in a ``finally`` so a refusal never leaves litter behind.

Mode bits are applied after staging and before the rename, in that order for a
mechanical reason: a directory at ``0555`` cannot be written into, so hardening
first would make the write impossible. The same fact is why ``remove`` restores
write permission across the tree before unlinking anything.

The result an operator can point at: module runtime that no agent can modify,
because the bytes are read-only and the directories holding them are not
writable by the account the agent runs as.

``module.installed`` and ``module.removed`` are emitted here rather than by the
caller, for the same reason the verify events are: this is where the outcome is
decided, so every surface that installs records the same fact.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from arcbundle._audit import emit_installed, emit_removed
from arcbundle._paths import require_safe_name
from arcbundle.errors import BundleMaterializeError
from arcbundle.verifier import VerifiedBundle

__all__ = ["DIR_MODE", "FILE_MODE", "materialize", "remove"]

_logger = logging.getLogger("arcbundle.materializer")

# Read and execute for everyone, write for no one — including the operator
# account that installed it, which is what makes an accidental in-place edit
# fail loudly instead of silently forking a signed module.
FILE_MODE = 0o444
DIR_MODE = 0o555

_STAGING_INFIX = ".staging-"
_BACKUP_INFIX = ".backup-"


def materialize(
    verified: VerifiedBundle,
    dest_root: Path,
    *,
    sink: Any | None = None,
    actor_did: str | None = None,
) -> Path:
    """Write a verified bundle to ``dest_root`` atomically and return its path.

    Args:
        verified: The bundle that passed verification, carrying the exact bytes
            that were hashed. Nothing is re-read from the bundle directory, so
            the content written is the content verified.
        dest_root: The deployment module root, outside every agent's tool fence.
        sink: Audit sink for ``module.installed``. ``None`` logs only.
        actor_did: Operator identity to attribute the install to.

    Returns:
        The materialized module directory, ``dest_root / <module>``.

    Raises:
        BundleMaterializeError: The destination root is unusable.
        OSError: A write, fsync, or rename failed. The destination is unchanged.
    """
    module = require_safe_name(verified.manifest.module, field="module")

    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(dir=dest_root, prefix=f".{module}{_STAGING_INFIX}"))
    except OSError as exc:
        raise BundleMaterializeError(f"cannot stage into {dest_root}: {exc}") from exc

    final = dest_root / module
    backup: Path | None = None
    try:
        _stage(staging, verified.files)
        _harden(staging)
        backup = _displace(final, staging_name=staging.name)
        try:
            os.replace(staging, final)
        except OSError:
            # The rename failed, so `final` is either still absent or sitting in
            # `backup`; put it back before the exception leaves this frame.
            if backup is not None:
                os.replace(backup, final)
                backup = None
            raise
        _fsync_dir(dest_root)
    finally:
        _discard(staging)
        if backup is not None:
            _discard(backup)

    emit_installed(
        bundle=verified.root,
        module=module,
        issuer=verified.manifest.issuer,
        version=verified.manifest.version,
        path=final,
        files=len(verified.files),
        sink=sink,
        actor_did=actor_did,
    )
    return final


def remove(
    name: str,
    dest_root: Path,
    *,
    sink: Any | None = None,
    actor_did: str | None = None,
) -> None:
    """Delete a materialized module tree, raising if it does not complete.

    Args:
        name: Module name — validated as a single path component, so a hostile
            config entry cannot aim this at a tree outside ``dest_root``.
        dest_root: The deployment module root.
        sink: Audit sink for ``module.removed``. ``None`` logs only.
        actor_did: Operator identity to attribute the removal to.

    Raises:
        BundleMaterializeError: The module is absent, is not a real directory,
            or any part of it survived the delete.
    """
    require_safe_name(name, field="module")
    target = dest_root / name

    if target.is_symlink() or not target.is_dir():
        raise BundleMaterializeError(f"no materialized module to remove at {target}")

    try:
        _make_writable(target)
        shutil.rmtree(target)
    except OSError as exc:
        raise BundleMaterializeError(f"could not remove {target}: {exc}") from exc

    if target.exists():
        raise BundleMaterializeError(f"{target} survived removal")

    emit_removed(module=name, path=target, sink=sink, actor_did=actor_did)


def _stage(staging: Path, files: Mapping[str, bytes]) -> None:
    """Write every payload file into the staging tree and flush it to the platter."""
    for relative in sorted(files):
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as handle:
            handle.write(files[relative])
            handle.flush()
            # Durability before the rename, not after: a rename that publishes
            # data the filesystem has not committed can survive a crash as a
            # complete-looking tree of empty files.
            os.fsync(handle.fileno())
    _fsync_dir(staging)


def _harden(root: Path) -> None:
    """Apply the read-only modes bottom-up so each directory is hardened last."""
    for current, _dirs, names in os.walk(root, topdown=False):
        for name in names:
            os.chmod(Path(current) / name, FILE_MODE)
        os.chmod(current, DIR_MODE)


def _displace(final: Path, *, staging_name: str) -> Path | None:
    """Move an existing install aside so the publishing rename has a free target.

    ``os.replace`` refuses a non-empty directory target, so a reinstall has to
    vacate the path first. Doing it by rename keeps the window between the old
    tree leaving and the new one arriving down to two atomic operations, and
    keeps the old tree recoverable if the second one fails.
    """
    if not final.exists() and not final.is_symlink():
        return None

    backup = final.parent / staging_name.replace(_STAGING_INFIX, _BACKUP_INFIX)
    os.replace(final, backup)
    return backup


def _fsync_dir(path: Path) -> None:
    """Flush a directory entry itself, so the names in it survive a crash."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _discard(path: Path) -> None:
    """Best-effort delete of a tree this module created, tolerating its absence."""
    if not path.exists():
        return
    try:
        _make_writable(path)
    except OSError as exc:
        # This runs in a `finally`; raising here would replace the failure the
        # caller actually needs to see with a cleanup detail.
        _logger.warning("could not restore write permission on %s: %s", path, exc)
    shutil.rmtree(path, ignore_errors=True)


def _make_writable(root: Path) -> None:
    """Restore owner write permission across a tree hardened to 0444 / 0555.

    Top-down: a directory has to be writable before its own entries can be
    unlinked, so it is widened on the way in rather than on the way out.
    """
    os.chmod(root, stat.S_IRWXU)
    for current, dirs, names in os.walk(root):
        for name in dirs:
            os.chmod(Path(current) / name, stat.S_IRWXU)
        for name in names:
            os.chmod(Path(current) / name, stat.S_IRUSR | stat.S_IWUSR)
