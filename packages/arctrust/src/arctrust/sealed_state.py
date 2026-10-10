"""Anchored, sealed, flock-protected persistence for one authority record (SPEC-087 COMP-002).

The local file is a cache of authority state, never the authority itself. An
independent monotonic anchor authenticates each revision by digest and carries
the sealed bytes as recovery intent, so:

* a crash between anchor advance and local replace is repaired from the intent;
* a file restored from further back than one revision is an unexplained
  rollback and is refused on every read;
* a file with no anchor behind it is refused.

Every directory, lock and file access is pinned to descriptors held under an
exclusive ``flock`` on ``.<record_name>.lock`` beside the file, so a swapped
directory, replaced lock or symlinked file fails closed instead of redirecting
the write. Changes are audited as ``<record_name>.change`` (durably when a
strict sink is given) and reads as ``<record_name>.read``; audit events carry
only the sealed digest, never the payload.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from arctrust.audit import AuditEvent, AuditSink, DurableAuditSink
from arctrust.byte_cipher import ByteCipher
from arctrust.monotonic import AnchorHead, MonotonicAnchor

_FILE_MODE = 0o600
_DIR_MODE = 0o700
_LOCK_TIMEOUT_SECONDS = 5
_RECORD_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class UserStoreError(RuntimeError):
    """The user store is unreadable, mis-permissioned, or malformed.

    Defined here, not in ``arctrust.users``, because ``users`` builds on this
    module; ``arctrust.users.UserStoreError`` is this same class.
    """


class AuthorityStateError(UserStoreError):
    """A sealed authority record failed integrity, rollback, anchor or file checks."""


class AuthorityCommitUncertainError(AuthorityStateError):
    """The revision is anchored and written, but its commit audit did not land.

    The new payload *is* the current authority; callers caching it must adopt it.
    """


class SealedAuthorityState:
    """Load and save one JSON authority record behind an anchor and a cipher."""

    def __init__(
        self,
        path: Path,
        *,
        anchor: MonotonicAnchor,
        cipher: ByteCipher,
        audit_sink: AuditSink,
        strict_audit_sink: DurableAuditSink | None,
        record_name: str,
        actor_did: str,
    ) -> None:
        if not _RECORD_NAME_RE.fullmatch(record_name):
            raise ValueError(f"invalid authority record name: {record_name!r}")
        self.path = path
        self._anchor = anchor
        self._cipher = cipher
        self._audit_sink = audit_sink
        self._strict_audit_sink = strict_audit_sink
        self._record_name = record_name
        self._label = f"{record_name} authority"
        self._lock_name = f".{record_name}.lock"
        self._actor_did = actor_did
        self._head: AnchorHead | None = None
        self._mutex = threading.RLock()
        self._dir_fd: int | None = None
        self._lock_fd: int | None = None

    # --- public contract ------------------------------------------------

    def load(
        self, validate: Callable[[dict[str, Any]], None] | None = None
    ) -> dict[str, Any] | None:
        """Return the anchored payload, or None when nothing was ever saved.

        ``validate`` runs before the read is audited and the head is adopted,
        so a record the caller rejects never becomes the base of a later save.
        """
        with self._serialized(), self._lock():
            return self._load_locked(validate)

    def save(self, payload: dict[str, Any]) -> None:
        """Seal, anchor and write ``payload`` as the next revision."""
        with self._serialized(), self._lock():
            self._save_locked(payload)

    def assert_current(self) -> None:
        """Refuse a writer whose last load is no longer the anchored head."""
        if self._anchor.latest() != self._head:
            self._audit("change", "deny", self._head.digest if self._head else "0" * 64)
            raise AuthorityStateError(f"stale {self._label} writer")

    # --- load -----------------------------------------------------------

    def _load_locked(
        self, validate: Callable[[dict[str, Any]], None] | None
    ) -> dict[str, Any] | None:
        head = self._anchor.latest()
        if head is None:
            if self._read_file() is not None or self._head is not None:
                raise AuthorityStateError(f"{self._label} anchor missing")
            return None
        if head.scope != self._anchor.scope:
            raise AuthorityStateError(f"{self._label} anchor scope mismatch")
        raw = self._read_verified(head)
        if not isinstance(raw, dict):
            raise AuthorityStateError(f"malformed {self._label} record")
        if validate is not None:
            validate(raw)
        self._audit("read", "allow", head.digest)
        self._head = head
        return raw

    def _verified_bytes(self, head: AnchorHead) -> bytes:
        sealed = self._read_file()
        if sealed is None or _digest(sealed) != head.digest:
            self._recover(head)
            sealed = self._read_file()
        if sealed is None:
            raise AuthorityStateError(f"{self._label} recovery failed")
        if _digest(sealed) != head.digest:
            raise AuthorityStateError(f"{self._label} integrity mismatch")
        return sealed

    def _read_verified(self, head: AnchorHead) -> object:
        try:
            sealed = self._verified_bytes(head)
            return json.loads(self._cipher.open(sealed.decode("ascii")))
        except (OSError, json.JSONDecodeError) as exc:
            raise AuthorityStateError(f"cannot read {self._label} state") from exc

    def _recover(self, head: AnchorHead) -> None:
        if not head.intent:
            raise AuthorityStateError(f"{self._label} recovery intent absent")
        sealed = head.intent.encode("ascii")
        if _digest(sealed) != head.digest:
            raise AuthorityStateError(f"{self._label} recovery intent invalid")
        existing = self._read_file()
        if existing is not None and _digest(existing) != head.previous_digest:
            raise AuthorityStateError(f"unexplained {self._label} rollback")
        self._write(sealed)

    # --- save -----------------------------------------------------------

    def _save_locked(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        sealed = self._cipher.seal(encoded)
        digest = _digest(sealed.encode("ascii"))
        self._audit("change", "attempt", digest)
        self._assert_pinned()
        head = self._anchor.compare_and_advance(self._head, digest, sealed)
        if self._anchor.latest() != head:
            raise AuthorityStateError(f"{self._label} advanced before local write")
        try:
            self._write(sealed.encode("ascii"))
        except OSError as exc:
            raise AuthorityStateError(f"{self._label} write outcome is uncertain") from exc
        self._head = head
        try:
            self._audit("change", "allow", digest)
        except Exception as exc:
            raise AuthorityCommitUncertainError(
                f"{self._label} committed; audit outcome is uncertain"
            ) from exc

    def _audit(self, verb: str, outcome: str, digest: str) -> None:
        event = AuditEvent(
            actor_did=self._actor_did,
            action=f"{self._record_name}.{verb}",
            target=self._anchor.scope,
            outcome=outcome,
            payload_hash=digest,
        )
        if verb == "change" and self._strict_audit_sink is not None:
            self._strict_audit_sink.write_durable(event)
        else:
            self._audit_sink.write(event)

    # --- locking and pinned file access ---------------------------------

    @contextmanager
    def _serialized(self) -> Iterator[None]:
        # One instance pins one descriptor pair at a time; threads sharing it queue here.
        if not self._mutex.acquire(timeout=_LOCK_TIMEOUT_SECONDS):
            raise AuthorityStateError(f"{self._label} instance is busy")
        try:
            yield
        finally:
            self._mutex.release()

    @contextmanager
    def _lock(self) -> Iterator[None]:
        # Callbacks unwind in reverse: unpin, unlock, close lock, close directory.
        with ExitStack() as stack:
            dir_fd = self._open_private_directory()
            stack.callback(os.close, dir_fd)
            lock_fd = self._open_private_lock(dir_fd)
            stack.callback(os.close, lock_fd)
            self._acquire_flock(lock_fd)
            stack.callback(fcntl.flock, lock_fd, fcntl.LOCK_UN)
            stack.callback(self._unpin)
            self._dir_fd, self._lock_fd = dir_fd, lock_fd
            self._assert_pinned()
            yield

    def _unpin(self) -> None:
        self._dir_fd = None
        self._lock_fd = None

    def _open_private_directory(self) -> int:
        directory = self.path.parent
        directory.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
        if directory.resolve() != directory.absolute() or directory.is_symlink():
            raise AuthorityStateError(f"{self._label} directory is a symlink")
        metadata = directory.stat()
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise AuthorityStateError(f"{self._label} directory is not private")
        return os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW)

    def _open_private_lock(self, dir_fd: int) -> int:
        lock_fd = os.open(
            self._lock_name,
            os.O_RDWR | os.O_NONBLOCK | os.O_CREAT | _NOFOLLOW,
            _FILE_MODE,
            dir_fd=dir_fd,
        )
        lock_meta = os.fstat(lock_fd)
        if (
            lock_meta.st_uid != os.getuid()
            or stat.S_IMODE(lock_meta.st_mode) != _FILE_MODE
            or not stat.S_ISREG(lock_meta.st_mode)
            or lock_meta.st_nlink != 1
        ):
            os.close(lock_fd)
            raise AuthorityStateError(f"{self._label} lock is not private")
        return lock_fd

    def _acquire_flock(self, lock_fd: int) -> None:
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise AuthorityStateError(f"{self._label} lock is busy") from None
                time.sleep(0.02)

    def _assert_pinned(self) -> int:
        dir_fd, lock_fd = self._dir_fd, self._lock_fd
        if dir_fd is None or lock_fd is None:
            raise AuthorityStateError(f"{self._label} lock is absent")
        directory = self.path.parent
        if directory.resolve() != directory.absolute():
            raise AuthorityStateError(f"{self._label} directory changed")
        try:
            directory_meta = os.stat(directory, follow_symlinks=False)
            lock_meta = os.stat(self._lock_name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError as exc:
            raise AuthorityStateError(f"{self._label} lock or directory changed") from exc
        if not _same_inode(directory_meta, os.fstat(dir_fd)):
            raise AuthorityStateError(f"{self._label} directory changed")
        pinned_lock = os.fstat(lock_fd)
        if not _same_inode(lock_meta, pinned_lock) or pinned_lock.st_nlink != 1:
            raise AuthorityStateError(f"{self._label} lock changed")
        return dir_fd

    def _read_file(self) -> bytes | None:
        dir_fd = self._assert_pinned()
        try:
            fd = os.open(self.path.name, os.O_RDONLY | os.O_NONBLOCK | _NOFOLLOW, dir_fd=dir_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise AuthorityStateError(f"{self._label} file is not a private regular file") from exc
        try:
            metadata = os.fstat(fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != _FILE_MODE
            ):
                raise AuthorityStateError(
                    f"{self._label} file has insecure permissions or file type"
                )
            with os.fdopen(fd, "rb", closefd=False) as stream:
                return stream.read()
        finally:
            os.close(fd)

    def _write(self, payload: bytes) -> None:
        dir_fd = self._assert_pinned()
        name = f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        tmp_fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, _FILE_MODE, dir_fd=dir_fd
        )
        try:
            with os.fdopen(tmp_fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            self._assert_pinned()
            os.replace(name, self.path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.fsync(dir_fd)
        finally:
            try:
                os.unlink(name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _same_inode(observed: os.stat_result, pinned: os.stat_result) -> bool:
    return (observed.st_dev, observed.st_ino) == (pinned.st_dev, pinned.st_ino)


__all__ = [
    "AuthorityCommitUncertainError",
    "AuthorityStateError",
    "SealedAuthorityState",
    "UserStoreError",
]
