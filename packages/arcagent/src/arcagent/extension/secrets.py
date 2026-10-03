"""SPEC-062 COMP-010 / P18-2 — the secret store seam for connector credentials.

One interface, :class:`SecretStore`, keyed by ``(connection, field)``. Since P18-2
every connector credential is held in a sealed arcstore custody row
(:mod:`arcagent.extension.custody`); there is no plaintext file backend and no
per-tier branch at a call site. The tier decides only which cipher seals the row
(:mod:`arcagent.extension.custody_select`).

Two properties are load-bearing rather than tidy:

* **A value only ever exists in the store.** Everything that crosses a boundary is
  a :class:`Secret`, whose ``repr``/``str``/``format`` render ``Secret(***)`` — so a
  credential interpolated into a log line, an exception, or a prompt renders as a
  placeholder instead of the token (REQ-265, LLM02/LLM07). Refusals name the
  coordinate and never echo the rejected material.
* **A coordinate is untrusted input.** It is bound into the ciphertext as
  associated data, so it is validated against a strict lowercase pattern:
  permitting ``Work`` alongside ``work`` would fold two connections onto one cell.

:class:`EnvFile` stays: it is the owner-only ``KEY=value`` file provider API keys
live in (``arc.env``), and the one-time migration of the legacy plaintext
connector file reads it through it.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import stat
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from arctrust import causal
from arctrust.audit import AuditEvent, AuditSink, emit

from arcagent.core.errors import ExtensionError
from arcagent.extension.coordinates import is_coordinate
from arcagent.extension.coordinates import refusal as coordinate_refusal

_MAX_ENV_FILE_BYTES = 1024 * 1024
_MAX_ENV_ENTRIES = 2048
_MAX_ENV_VALUE_CHARS = 64 * 1024


#: What a credential is replaced with wherever a third party's own words are rendered.
REDACTED = "***"


def redact(text: str, values: Iterable[str]) -> str:
    """Take known credential values back out of text Arc did not write.

    Not belt-and-braces. Every place this is used renders the output of a program
    Arc started on an extension's behalf, and several CLIs echo the credential they
    were given straight back — into a line that is logged, shown in a browser, and
    (for a tool result) put in front of a model (LLM02).

    An empty value is skipped: replacing ``""`` would insert the placeholder between
    every character.
    """
    for value in values:
        if value:
            text = text.replace(value, REDACTED)
    return text


class Secret:
    """A credential value that renders as a placeholder wherever it is formatted.

    The only way to the value is :meth:`reveal`, which makes every place a
    credential is deliberately used greppable — and makes every place one is
    *accidentally* interpolated render ``Secret(***)`` instead.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        """Return the underlying value. Call this as late as possible."""
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return repr(self)


@dataclass(frozen=True)
class SecretRef:
    """Where one credential lives: which connection, which field.

    Keyed by the connection and NOT by the agent, which is the property that makes
    a grant a grant. One connected account has one credential: granting it to a
    second agent copies nothing, so there is no second copy to rotate and none to
    leave behind on a revoke. An agent never appears in this coordinate at all,
    so there is no path by which "which agent is asking" could select a different
    stored value.
    """

    connection: str
    field: str

    def __post_init__(self) -> None:
        for name, value in (("connection", self.connection), ("field", self.field)):
            if not is_coordinate(value):
                raise ExtensionError(
                    code="SECRET_REF_INVALID",
                    message=coordinate_refusal(f"secret {name}", value),
                    details={"coordinate": name},
                )

    def __str__(self) -> str:
        return f"{self.connection}/{self.field}"


class SecretBackend(Protocol):
    """The one seam a deployment swaps. Values in, values out, nothing rendered."""

    async def get(self, ref: SecretRef) -> str | None: ...

    async def put(self, ref: SecretRef, value: str) -> None: ...

    async def delete(self, ref: SecretRef) -> bool: ...

    async def present(self, connection: str) -> frozenset[str]: ...


class EnvFile:
    """One owner-only ``KEY=value`` file, read and rewritten safely (D-582).

    Every mutation rewrites the whole file through a private temp file and one
    ``os.replace``. An in-process lock and a sibling advisory lock cover the
    complete read-modify-replace transaction, so concurrent processes cannot
    overwrite a newer snapshot.

    Provider API keys (:class:`arcagent.keys.KeyStore`) live in one of these, and
    the one-time migration reads the legacy plaintext connector file
    through it, so its ownership and ``O_NOFOLLOW`` checks apply there too.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        """Where this store lives — what a surface tells the operator to inspect."""
        return self._path

    async def read(self) -> dict[str, str]:
        """Every entry, or an empty mapping when the file was never written."""
        async with self._lock:
            return await asyncio.to_thread(self._read_transaction)

    async def put(self, key: str, value: str) -> None:
        if not key or "=" in key or "\n" in key:
            raise ValueError("env key must be non-empty and contain no '=' or newline")
        if len(value) > _MAX_ENV_VALUE_CHARS or "\n" in value:
            raise ValueError("env value is too large or contains a newline")
        async with self._lock:
            await asyncio.to_thread(self._put_transaction, key, value)

    async def delete(self, key: str) -> bool:
        """Drop one entry. False when there was nothing to drop."""
        async with self._lock:
            return await asyncio.to_thread(self._delete_transaction, key)

    @contextlib.contextmanager
    def _file_lock(self, *, exclusive: bool) -> Iterator[None]:
        parent = self._path.parent
        parent.mkdir(parents=True, exist_ok=True)
        parent.chmod(0o700)
        lock_path = parent / f".{self._path.name}.lock"
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read_transaction(self) -> dict[str, str]:
        with self._file_lock(exclusive=False):
            return self._read()

    def _put_transaction(self, key: str, value: str) -> None:
        with self._file_lock(exclusive=True):
            entries = self._read()
            if key not in entries and len(entries) >= _MAX_ENV_ENTRIES:
                raise ValueError("env file entry limit exceeded")
            entries[key] = value
            self._write(entries)

    def _delete_transaction(self, key: str) -> bool:
        with self._file_lock(exclusive=True):
            entries = self._read()
            if entries.pop(key, None) is None:
                return False
            self._write(entries)
            return True

    def _read(self) -> dict[str, str]:
        """Parse the store, refusing a file whose permissions have been loosened."""
        raw = self._read_owned()
        entries: dict[str, str] = {}
        for line in raw.splitlines():
            key, separator, value = line.partition("=")
            if separator and key:
                entries[key] = value
        return entries

    def _read_owned(self) -> str:
        """Read the whole file only if it is 0600 and owned by this user.

        ``utils.secure_file.read_secret_owned`` implements the same fd recipe but
        caps the read at 4096 bytes and answers with a reason string; a store
        holding every connector's credentials outgrows both.
        """
        try:
            fd = os.open(str(self._path), os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return ""
        except OSError as exc:
            raise self._refuse("SECRET_STORE_UNREADABLE", f"errno {exc.errno}") from exc
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid():
                raise self._refuse("SECRET_STORE_WRONG_OWNER", "owned by another user")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise self._refuse(
                    "SECRET_STORE_LOOSE_PERMS",
                    f"mode is {stat.S_IMODE(info.st_mode):o}, expected 600",
                )
            chunks: list[bytes] = []
            total = 0
            while chunk := os.read(fd, 65536):
                total += len(chunk)
                if total > _MAX_ENV_FILE_BYTES:
                    raise self._refuse("SECRET_STORE_TOO_LARGE", "store exceeds size limit")
                chunks.append(chunk)
        finally:
            os.close(fd)
        return b"".join(chunks).decode("utf-8")

    def _write(self, entries: dict[str, str]) -> None:
        """Replace the store atomically, 0600 from creation, inside a 0700 directory."""
        parent = self._path.parent
        parent.mkdir(parents=True, exist_ok=True)
        parent.chmod(0o700)
        body = "".join(f"{key}={value}\n" for key, value in entries.items())
        if len(entries) > _MAX_ENV_ENTRIES or len(body.encode("utf-8")) > _MAX_ENV_FILE_BYTES:
            raise ValueError("env file limits exceeded")

        # Unique per write, not per process: two store instances over the same file
        # (a CLI verb beside a running module) must not collide on the temp name.
        temp = parent / f".{self._path.name}.tmp.{os.getpid()}.{os.urandom(6).hex()}"
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, body.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(str(temp), str(self._path))
        except OSError:
            temp.unlink(missing_ok=True)
            raise

    def _refuse(self, code: str, reason: str) -> ExtensionError:
        return ExtensionError(
            code=code,
            message=f"refusing to read the secret store at {self._path}: {reason}",
            details={"path": str(self._path), "reason": reason},
        )


class SecretStore:
    """The one interface connector code calls, whichever store is behind it."""

    def __init__(self, backend: SecretBackend, *, sink: AuditSink | None = None) -> None:
        self._backend = backend
        self._sink = sink

    async def get(self, ref: SecretRef) -> Secret | None:
        """Resolve a credential, or None if it was never stored."""
        value = await self._backend.get(ref)
        self._audit("secret.read", ref, "allow" if value else "not_found")
        return Secret(value) if value is not None else None

    async def put(self, ref: SecretRef, value: str) -> None:
        """Store a credential. The value never leaves the backend it is written to."""
        self._validate(ref, value)
        await self._backend.put(ref, value)
        self._audit("secret.write", ref, "allow")

    async def present(self, connection: str) -> frozenset[str]:
        """Which fields are stored for ``connection``. Names only; nothing is opened."""
        return await self._backend.present(connection)

    async def delete(self, ref: SecretRef) -> bool:
        """Forget a credential. True when one was removed."""
        removed = await self._backend.delete(ref)
        self._audit("secret.delete", ref, "allow" if removed else "not_found")
        return removed

    @staticmethod
    def _validate(ref: SecretRef, value: str) -> None:
        """Refuse empty values and control characters, naming only the coordinate.

        A line break or NUL in a credential is never legitimate and is how a value
        smuggles a second header or argument into whatever consumes it.
        """
        if not value:
            raise ExtensionError(
                code="SECRET_VALUE_EMPTY",
                message=f"refusing to store an empty value for {ref}",
                details={"secret": str(ref)},
            )
        if any(character in value for character in "\n\r\x00"):
            raise ExtensionError(
                code="SECRET_VALUE_INVALID",
                message=f"the value for {ref} contains a line break or NUL and was refused",
                details={"secret": str(ref)},
            )

    @property
    def _store_name(self) -> str:
        return str(getattr(self._backend, "store_name", type(self._backend).__name__))

    def _audit(self, action: str, ref: SecretRef, outcome: str) -> None:
        """Record the credential carve-out: coordinates, initiator, outcome — no value."""
        if self._sink is None:
            return
        emit(
            AuditEvent(
                actor_did=causal.actor_did(),
                action=action,
                target=f"secret:{ref}",
                outcome=outcome,
                extra={"store": self._store_name},
            ),
            self._sink,
        )


__all__ = [
    "REDACTED",
    "EnvFile",
    "Secret",
    "SecretBackend",
    "SecretRef",
    "SecretStore",
    "redact",
]
