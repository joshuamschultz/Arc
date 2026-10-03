"""P18-2 — the one-time move of ``connections.env`` into sealed custody.

Before P18-2 every connector credential lived in plaintext in the deployment's
owner-only ``connections.env``. This module is the ONLY code that still names that
file. It reads it once, writes every declared credential into sealed custody,
reads every value back through a FRESH store and compares in constant time, and
only then deletes the file. Undeclared leftovers (orphan keys from a removed or
renamed bundle) are dropped by name; their values are never moved.

The migration fails closed: if any value cannot be stored or read back, the file
is left exactly where it was and the caller refuses to start (arcui) or reports
the failure (``arc connector migrate-secrets``).
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from arctrust.audit import AuditEvent, AuditSink, emit
from arctrust.paths import config_file

from arcagent.core.errors import ExtensionError
from arcagent.extension.custody import CredentialCipher, CredentialRowStore
from arcagent.extension.grants import ConnectionRegistry
from arcagent.extension.secrets import EnvFile, SecretRef, SecretStore

#: The legacy file, beside ``connections.toml``. Named here and nowhere else.
LEGACY_ENV_FILENAME = "connections.env"

#: Actor recorded when arcui migrates at startup with no operator present.
MIGRATOR_DID = "did:arc:system:migrator"

#: How the deleted file backend spelled a coordinate as an env key.
_LEGACY_PREFIX = "ARC_SECRET"

#: Declared fields per connection, or ``None`` for a connection whose bundle no
#: longer plans (its keys are then orphans).
DeclaredFields = Callable[[str, str], tuple[str, ...] | None]


def legacy_env_path(arc_dir: Path) -> Path:
    """Where the pre-P18-2 credential file lived for a deployment."""
    return config_file(LEGACY_ENV_FILENAME, arc_dir)


def _legacy_key(connection: str, field_name: str) -> str:
    return f"{_LEGACY_PREFIX}_{connection}_{field_name}".upper()


@dataclass(frozen=True)
class MigrationReport:
    """What the migration did. Names only; never a value."""

    skipped: bool = False
    dry_run: bool = False
    #: ``connection/field`` coordinates moved into custody.
    migrated: tuple[str, ...] = ()
    #: Legacy key names present in the file and declared by no connection.
    dropped: tuple[str, ...] = ()
    connections: tuple[str, ...] = ()
    deleted: bool = False
    path: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)


def _plan_keys(
    registry: ConnectionRegistry, declared_fields: DeclaredFields
) -> dict[str, SecretRef]:
    """Legacy env key -> the coordinate it holds, for every declared field."""
    keys: dict[str, SecretRef] = {}
    for instance, connection in registry.all().items():
        fields = declared_fields(instance, connection.extension)
        for name in fields or ():
            keys[_legacy_key(instance, name)] = SecretRef(connection=instance, field=name)
    return keys


async def migrate_connector_secrets(
    *,
    env_path: Path,
    registry: ConnectionRegistry,
    declared_fields: DeclaredFields,
    secret_store: SecretStore | None,
    verify_store: Callable[[], SecretStore] | None,
    actor_did: str,
    sink: AuditSink,
    dry_run: bool = False,
) -> MigrationReport:
    """Move ``env_path`` into sealed custody, verify, delete, audit.

    Raises:
        ExtensionError: ``MIGRATION_VERIFY_FAILED`` when a value read back from a
            fresh store differs (the file is kept), or the store's own refusal
            (no cipher, a symlinked or loosely-permissioned file) unchanged.
    """
    if not os.path.lexists(env_path):
        return MigrationReport(skipped=True, path=str(env_path))
    # EnvFile keeps the O_NOFOLLOW + owner + mode checks: a symlinked or
    # world-readable file is refused, never read.
    entries = await EnvFile(env_path).read()
    keys = _plan_keys(registry, declared_fields)
    to_move = {key: keys[key] for key, value in entries.items() if key in keys and value}
    dropped = tuple(sorted(key for key in entries if key not in keys))
    connections = tuple(sorted({ref.connection for ref in to_move.values()}))
    migrated = tuple(sorted(str(ref) for ref in to_move.values()))
    if dry_run:
        return MigrationReport(
            dry_run=True,
            migrated=migrated,
            dropped=dropped,
            connections=connections,
            path=str(env_path),
        )
    if secret_store is None or verify_store is None:
        raise ExtensionError(
            code="SECRET_STORE_UNCONFIGURED",
            message="no credential custody is available to migrate connections.env into",
            details={"path": str(env_path)},
        )
    for key, ref in to_move.items():
        await secret_store.put(ref, entries[key])
    await _verify(to_move, entries, verify_store())
    _delete(env_path)
    emit(
        AuditEvent(
            actor_did=actor_did,
            action="connection.credential.migrated",
            target="secret:connections.env",
            outcome="allow",
            extra={
                "count": len(to_move),
                "dropped": list(dropped),
                "connections": list(connections),
            },
        ),
        sink,
    )
    return MigrationReport(
        migrated=migrated,
        dropped=dropped,
        connections=connections,
        deleted=True,
        path=str(env_path),
    )


async def _verify(
    moved: Mapping[str, SecretRef],
    entries: Mapping[str, str],
    store: SecretStore,
) -> None:
    for key, ref in moved.items():
        found = await store.get(ref)
        stored = found.reveal() if found is not None else ""
        if not hmac.compare_digest(stored.encode(), entries[key].encode()):
            raise ExtensionError(
                code="MIGRATION_VERIFY_FAILED",
                message=(
                    f"{ref} did not read back from custody as written; connections.env was "
                    "kept and nothing else changed"
                ),
                details={"secret": str(ref)},
            )


def _delete(env_path: Path) -> None:
    """Unlink and fsync the directory so the removal is durable.

    Overwrite-before-unlink buys nothing on SSD/COW filesystems, so it is not done.
    """
    env_path.unlink()
    lock = env_path.parent / f".{env_path.name}.lock"
    lock.unlink(missing_ok=True)
    fd = os.open(str(env_path.parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class ResealReport:
    """What a re-seal did. Connection names only; never a value."""

    resealed: tuple[str, ...] = ()
    #: Rows already under the target cipher (a re-run skips them).
    already: tuple[str, ...] = ()
    dry_run: bool = False
    #: Rows a dry run would re-seal.
    pending: tuple[str, ...] = ()


async def reseal_connector_secrets(
    rows: CredentialRowStore,
    *,
    source: CredentialCipher,
    actor_did: str,
    sink: AuditSink,
    dry_run: bool = False,
) -> ResealReport:
    """Re-seal every ``source``-sealed custody row under ``rows``' cipher (P18-2F).

    Used when a deployment moves from ``in_process`` to ``vault_transit``
    custody. Each row moves in ONE verified compare-and-set
    (:meth:`CredentialRowStore.reseal`), so the job is crash-safe (a crash leaves
    every row wholly under one cipher) and idempotent (a re-run skips rows
    already moved). The first refusal stops the run; rows already moved stay
    moved and a re-run continues from there.

    Raises:
        ExtensionError: ``CREDENTIAL_CUSTODY_UNAVAILABLE`` (the transit cannot
            answer), ``CREDENTIAL_UNREADABLE`` (a row sealed under neither key),
            or ``CREDENTIAL_STORE_BUSY``.
    """
    names = await rows.connections()
    if dry_run:
        pending = []
        for name in names:
            row = await rows.read(name)
            if row is not None and row.cipher == source.kind:
                pending.append(name)
        return ResealReport(dry_run=True, pending=tuple(pending))
    resealed: list[str] = []
    already: list[str] = []
    for name in names:
        moved = await rows.reseal(name, source=source, actor_did=actor_did)
        (resealed if moved else already).append(name)
    emit(
        AuditEvent(
            actor_did=actor_did,
            action="connection.credential.resealed_all",
            target="secret:connector_credentials",
            outcome="allow",
            extra={"count": len(resealed), "connections": resealed, "to": rows.cipher_kind},
        ),
        sink,
    )
    return ResealReport(resealed=tuple(resealed), already=tuple(already))


__all__ = [
    "LEGACY_ENV_FILENAME",
    "MIGRATOR_DID",
    "DeclaredFields",
    "MigrationReport",
    "ResealReport",
    "legacy_env_path",
    "migrate_connector_secrets",
    "reseal_connector_secrets",
]
