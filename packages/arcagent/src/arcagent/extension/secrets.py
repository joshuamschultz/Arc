"""SPEC-062 COMP-010 — the secret store seam for connector credentials.

One interface, :class:`SecretStore`, keyed by ``(agent, instance, field)``. Which
store backs it is a tier decision made once in :func:`select_secret_backend`, never
a branch at a call site (REQ-294): personal keeps credentials in the agent's own
``arc.env`` at owner-only permissions, enterprise and federal point the same calls
at an external vault.

Three properties are load-bearing rather than tidy:

* **A value only ever exists in the store.** Everything that crosses a boundary is
  a :class:`Secret`, whose ``repr``/``str``/``format`` render ``Secret(***)`` — so a
  credential interpolated into a log line, an exception, or a prompt renders as a
  placeholder instead of the token (REQ-265, LLM02/LLM07). Refusals name the
  coordinate and never echo the rejected material.
* **A coordinate is untrusted input.** It becomes an environment key and a vault
  path, so it is validated against a strict lowercase pattern. Lowercase is not
  cosmetic: the local backend upper-cases coordinates into an env key, and
  permitting ``Work`` alongside ``work`` would fold two connections onto one cell
  — one account silently reading another's credential.
* **A write is atomic and never a downgrade.** The local backend writes a private
  temp file, fsyncs, and ``os.replace``s it, so an interrupted write leaves the
  previous store rather than a truncated one (the torn-credential half of
  REQ-288). A vault that cannot accept a write refuses loudly instead of falling
  back to a local file, which would quietly undo an operator's hardening.

The env-file shape follows ``arcgateway/connect.py`` — the existing writer for a
credential the gateway reads — so an operator sees one file format, not two.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from arctrust.audit import AuditEvent, AuditSink, emit

from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.core.vault import VaultBackend, VaultUnreachable
from arcagent.extension.coordinates import is_coordinate
from arcagent.extension.coordinates import refusal as coordinate_refusal

_logger = logging.getLogger("arcagent.extension.secrets")

#: Prefix for the env keys this store owns, so an operator can see at a glance which
#: entries in ``arc.env`` are connector credentials.
_ENV_PREFIX = "ARC_SECRET"

#: Root of the vault namespace this store owns.
_VAULT_ROOT = "arc/connectors"


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
    """Where one credential lives: which agent, which connected instance, which field."""

    agent: str
    instance: str
    field: str

    def __post_init__(self) -> None:
        for name, value in (
            ("agent", self.agent),
            ("instance", self.instance),
            ("field", self.field),
        ):
            if not is_coordinate(value):
                raise ExtensionError(
                    code="SECRET_REF_INVALID",
                    message=coordinate_refusal(f"secret {name}", value),
                    details={"coordinate": name},
                )

    @property
    def env_key(self) -> str:
        """The env-file key for this coordinate, as ``arc.env`` stores it."""
        return f"{_ENV_PREFIX}_{self.agent}_{self.instance}_{self.field}".upper()

    @property
    def vault_path(self) -> str:
        """The vault path for this coordinate."""
        return f"{_VAULT_ROOT}/{self.agent}/{self.instance}/{self.field}"

    def __str__(self) -> str:
        return f"{self.agent}/{self.instance}/{self.field}"


class SecretBackend(Protocol):
    """The one seam a deployment swaps. Values in, values out, nothing rendered."""

    async def get(self, ref: SecretRef) -> str | None: ...

    async def put(self, ref: SecretRef, value: str) -> None: ...

    async def delete(self, ref: SecretRef) -> bool: ...


@runtime_checkable
class WritableVault(Protocol):
    """A vault that also accepts writes — what unattended renewal requires."""

    async def set_secret(self, path: str, value: str) -> None: ...

    async def delete_secret(self, path: str) -> bool: ...


class EnvFile:
    """One owner-only ``KEY=value`` file, read and rewritten safely (D-582).

    Every mutation rewrites the whole file through a private temp file and one
    ``os.replace``, guarded by a lock so two coroutines cannot lose each other's
    entry in a read-modify-write. A second *process* touching the same file can
    still lose its own update, but ``os.replace`` means it can never leave a torn
    one.

    The recipe is shared rather than copied: connector credentials
    (:class:`LocalFileSecretBackend`) and provider API keys
    (:class:`arcagent.keys.KeyStore`) are the same kind of file with the same
    exposure, and a second implementation is a second place to get a permission
    bit wrong.
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
            return await asyncio.to_thread(self._read)

    async def put(self, key: str, value: str) -> None:
        async with self._lock:
            entries = await asyncio.to_thread(self._read)
            entries[key] = value
            await asyncio.to_thread(self._write, entries)

    async def delete(self, key: str) -> bool:
        """Drop one entry. False when there was nothing to drop."""
        async with self._lock:
            entries = await asyncio.to_thread(self._read)
            if entries.pop(key, None) is None:
                return False
            await asyncio.to_thread(self._write, entries)
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
            while chunk := os.read(fd, 65536):
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


class LocalFileSecretBackend:
    """The default store: one owner-only env file in the agent's own home (D-555).

    Each agent owns its own file, so contention on it is an operator running the
    CLI against a live agent rather than routine.
    """

    def __init__(self, env_file: Path) -> None:
        self._file = EnvFile(env_file)

    async def get(self, ref: SecretRef) -> str | None:
        return (await self._file.read()).get(ref.env_key)

    async def put(self, ref: SecretRef, value: str) -> None:
        await self._file.put(ref.env_key, value)

    async def delete(self, ref: SecretRef) -> bool:
        return await self._file.delete(ref.env_key)


class VaultSecretBackend:
    """The external store enterprise and federal deployments point the same calls at.

    Reads go through the existing :class:`~arcagent.core.vault.VaultBackend`
    Protocol, so every backend the project already has works unchanged. Writes
    need more than that Protocol offers; a vault that cannot take one refuses
    rather than letting the caller fall back to a file on the host.
    """

    def __init__(self, vault: VaultBackend) -> None:
        self._vault = vault

    async def get(self, ref: SecretRef) -> str | None:
        try:
            return await self._vault.get_secret(ref.vault_path)
        except VaultUnreachable as exc:
            raise ExtensionError(
                code="SECRET_STORE_UNREACHABLE",
                message=f"the vault holding {ref} could not be reached",
                details={"secret": str(ref)},
            ) from exc

    async def put(self, ref: SecretRef, value: str) -> None:
        await self._writable(ref).set_secret(ref.vault_path, value)

    async def delete(self, ref: SecretRef) -> bool:
        return await self._writable(ref).delete_secret(ref.vault_path)

    def _writable(self, ref: SecretRef) -> WritableVault:
        if not isinstance(self._vault, WritableVault):
            raise ExtensionError(
                code="SECRET_STORE_READ_ONLY",
                message=(
                    f"the configured vault cannot store {ref}; provision the secret in "
                    f"the vault at {ref.vault_path} instead"
                ),
                details={"secret": str(ref), "vault_path": ref.vault_path},
            )
        return self._vault


class SecretStore:
    """The one interface connector code calls, whichever store is behind it."""

    def __init__(self, backend: SecretBackend, *, sink: AuditSink | None = None) -> None:
        self._backend = backend
        self._sink = sink

    async def get(self, ref: SecretRef, *, caller_did: str) -> Secret | None:
        """Resolve a credential, or None if it was never stored."""
        value = await self._backend.get(ref)
        self._audit("secret.read", ref, caller_did, "allow" if value else "not_found")
        return Secret(value) if value is not None else None

    async def put(self, ref: SecretRef, value: str, *, caller_did: str) -> None:
        """Store a credential. The value never leaves the backend it is written to."""
        self._validate(ref, value)
        await self._backend.put(ref, value)
        self._audit("secret.write", ref, caller_did, "allow")

    async def delete(self, ref: SecretRef, *, caller_did: str) -> bool:
        """Forget a credential. True when one was removed."""
        removed = await self._backend.delete(ref)
        self._audit("secret.delete", ref, caller_did, "allow" if removed else "not_found")
        return removed

    @staticmethod
    def _validate(ref: SecretRef, value: str) -> None:
        """Refuse material that could forge a second entry, naming only the coordinate.

        The local store is line-oriented, so an unchecked newline in a value is a
        write to a key the caller did not ask for.
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

    def _audit(self, action: str, ref: SecretRef, caller_did: str, outcome: str) -> None:
        """Record the credential carve-out: coordinates, caller, outcome — no value."""
        if self._sink is None:
            return
        emit(
            AuditEvent(
                actor_did=caller_did,
                action=action,
                target=f"secret:{ref}",
                outcome=outcome,
                extra={"store": type(self._backend).__name__},
            ),
            self._sink,
        )


def select_secret_backend(
    tier: Tier, *, env_file: Path | None = None, vault: VaultBackend | None = None
) -> SecretBackend:
    """Choose the backing store for a deployment. The only place tier is read.

    A configured vault always wins. Without one, federal refuses rather than
    writing a credential to the host — silently downgrading the store an operator
    hardened is the failure this guard exists to prevent.
    """
    if vault is not None:
        return VaultSecretBackend(vault)
    if tier is Tier.FEDERAL:
        raise ExtensionError(
            code="SECRET_STORE_VAULT_REQUIRED",
            message="federal deployments must configure an external vault for connector secrets",
            details={"tier": str(tier)},
        )
    if env_file is None:
        raise ExtensionError(
            code="SECRET_STORE_UNCONFIGURED",
            message="no vault and no per-agent secret file were configured",
            details={"tier": str(tier)},
        )
    if tier is Tier.ENTERPRISE:
        _logger.warning(
            "no vault configured; connector secrets for this deployment are stored at %s",
            env_file,
        )
    return LocalFileSecretBackend(env_file)


__all__ = [
    "EnvFile",
    "LocalFileSecretBackend",
    "Secret",
    "SecretBackend",
    "SecretRef",
    "SecretStore",
    "VaultSecretBackend",
    "WritableVault",
    "select_secret_backend",
]
