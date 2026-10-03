"""P18-2 — the one-time move of ``connections.env`` into sealed custody.

Before P18-2 every connector credential lived in plaintext in the deployment's
owner-only ``connections.env``. This module is the ONLY code that still names that
file. It reads it once and accounts for EVERY non-empty value in it:

* a field a connection's bundle declares is written into sealed custody and read
  back through a FRESH store (constant-time compare);
* a legacy OAuth app pair a bundle names (``[oauth] legacy_client_id_env`` /
  ``legacy_client_secret_env``) is written into the provider's sealed app slot and
  read back;
* anything else is *unresolved*: no connection declares it, it disagrees with what
  custody or the app slot already holds, or it is half an app pair.

A value is never dropped silently. Any unresolved value refuses the migration (the
file is kept, nothing is written) unless the operator asked for
``drop_undeclared``, and then each dropped key is audited by name. Only when every
value is moved-and-verified or explicitly dropped is the file deleted. There is no
plaintext backup: the refusal itself keeps the original file until it is resolved.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from arctrust.audit import AuditEvent, AuditSink, emit
from arctrust.paths import config_file

from arcagent.core.errors import ExtensionError
from arcagent.extension.custody import CredentialCipher, CredentialRowStore
from arcagent.extension.grants import ConnectionRegistry
from arcagent.extension.oauth_apps import OAuthAppStore
from arcagent.extension.secrets import EnvFile, SecretRef, SecretStore

#: The legacy file, beside ``connections.toml``. Named here and nowhere else.
LEGACY_ENV_FILENAME = "connections.env"

#: Actor recorded when arcui migrates at startup with no operator present.
MIGRATOR_DID = "did:arc:system:migrator"

#: The refusal when a non-empty value would be lost.
MIGRATION_UNDECLARED_KEYS = "MIGRATION_UNDECLARED_KEYS"

#: How the deleted file backend spelled a coordinate as an env key.
_LEGACY_PREFIX = "ARC_SECRET"

#: Why a value could not be moved (names only; never a value).
UNDECLARED = "undeclared"
CUSTODY_DIFFERS = "custody_differs"
APP_SLOT_DIFFERS = "app_slot_differs"
APP_PAIR_INCOMPLETE = "app_pair_incomplete"
APP_VALUES_DISAGREE = "app_values_disagree"

#: Declared fields per connection, or ``None`` for a connection whose bundle no
#: longer plans (its keys are then unresolved).
DeclaredFields = Callable[[str, str], tuple[str, ...] | None]


@dataclass(frozen=True)
class LegacyApp:
    """A bundle's pre-app-slot spelling of its provider's client id and secret."""

    provider: str
    client_id_suffix: str
    client_secret_suffix: str


#: The legacy app pair per connection, or ``None`` when its bundle names none.
LegacyApps = Callable[[str, str], LegacyApp | None]


def legacy_env_path(arc_dir: Path) -> Path:
    """Where the pre-P18-2 credential file lived for a deployment."""
    return config_file(LEGACY_ENV_FILENAME, arc_dir)


def _legacy_key(connection: str, field_name: str) -> str:
    return f"{_LEGACY_PREFIX}_{connection}_{field_name}".upper()


def _legacy_spellings(connection: str, suffix: str) -> tuple[str, str]:
    """Both ways a legacy app value was keyed: bare and secret-store prefixed."""
    return f"{connection}_{suffix}".upper(), _legacy_key(connection, suffix)


@dataclass(frozen=True)
class MigrationReport:
    """What the migration did, or (dry run) would do. Names only; never a value."""

    skipped: bool = False
    dry_run: bool = False
    #: ``connection/field`` coordinates written into custody.
    migrated: tuple[str, ...] = ()
    #: Coordinates custody already holds with the same value (nothing written).
    already: tuple[str, ...] = ()
    #: Providers whose OAuth app slot was (or would be) set from the file.
    apps: tuple[str, ...] = ()
    #: Legacy keys accounted for by an app slot (set now, or already equal).
    app_keys: tuple[str, ...] = ()
    #: ``(key, reason)`` for every non-empty value that would be lost.
    unresolved: tuple[tuple[str, str], ...] = ()
    #: Keys dropped on purpose (``drop_undeclared``), each audited.
    dropped: tuple[str, ...] = ()
    #: Keys whose value was empty: they held no credential.
    empty: tuple[str, ...] = ()
    connections: tuple[str, ...] = ()
    deleted: bool = False
    path: str = ""


@dataclass
class _Plan:
    moves: dict[str, SecretRef] = field(default_factory=dict)
    already: dict[str, SecretRef] = field(default_factory=dict)
    #: provider -> (client id, client secret) to write.
    apps: dict[str, tuple[str, str]] = field(default_factory=dict)
    app_keys: set[str] = field(default_factory=set)
    unresolved: dict[str, str] = field(default_factory=dict)
    empty: tuple[str, ...] = ()


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


def _app_candidates(
    registry: ConnectionRegistry, legacy_apps: LegacyApps, declared: Mapping[str, SecretRef]
) -> dict[str, dict[str, set[str]]]:
    """provider -> {"id": keys, "secret": keys} a legacy app value may sit under."""
    found: dict[str, dict[str, set[str]]] = {}
    for instance, connection in registry.all().items():
        legacy = legacy_apps(instance, connection.extension)
        if legacy is None:
            continue
        roles = found.setdefault(legacy.provider, {"id": set(), "secret": set()})
        roles["id"].update(_legacy_spellings(instance, legacy.client_id_suffix))
        roles["secret"].update(_legacy_spellings(instance, legacy.client_secret_suffix))
    for roles in found.values():
        for keys in roles.values():
            keys.difference_update(declared)
    return found


async def _classify_declared(
    plan: _Plan,
    values: Mapping[str, str],
    declared: Mapping[str, SecretRef],
    reader: SecretStore | None,
) -> None:
    for key, value in values.items():
        ref = declared.get(key)
        if ref is None:
            continue
        found = await reader.get(ref) if reader is not None else None
        if found is None:
            plan.moves[key] = ref
        elif hmac.compare_digest(found.reveal().encode(), value.encode()):
            plan.already[key] = ref
        else:
            plan.unresolved[key] = CUSTODY_DIFFERS


async def _classify_app(
    plan: _Plan,
    provider: str,
    present: Mapping[str, set[str]],
    values: Mapping[str, str],
    apps: OAuthAppStore | None,
) -> None:
    keys = present["id"] | present["secret"]
    ids = {values[key].strip() for key in present["id"]}
    secrets = {values[key].strip() for key in present["secret"]}
    reason = _app_pair_problem(ids, secrets)
    if reason is None:
        client_id, client_secret = next(iter(ids)), next(iter(secrets))
        existing = await apps.get(provider) if apps is not None else None
        if existing is None:
            plan.apps[provider] = (client_id, client_secret)
        elif not _same_app(existing.client_id, existing.client_secret.reveal(), ids, secrets):
            reason = APP_SLOT_DIFFERS
    if reason is None:
        plan.app_keys |= keys
        return
    for key in keys:
        plan.unresolved[key] = reason


def _app_pair_problem(ids: set[str], secrets: set[str]) -> str | None:
    if not ids or not secrets:
        return APP_PAIR_INCOMPLETE
    if len(ids) > 1 or len(secrets) > 1:
        return APP_VALUES_DISAGREE
    return None


def _same_app(client_id: str, client_secret: str, ids: set[str], secrets: set[str]) -> bool:
    return hmac.compare_digest(client_id.encode(), next(iter(ids)).encode()) and (
        hmac.compare_digest(client_secret.encode(), next(iter(secrets)).encode())
    )


async def _plan(
    entries: Mapping[str, str],
    *,
    registry: ConnectionRegistry,
    declared_fields: DeclaredFields,
    legacy_apps: LegacyApps,
    reader: SecretStore | None,
    apps: OAuthAppStore | None,
) -> _Plan:
    """Account for every key in the file: move, map to an app slot, or unresolved."""
    values = {key: value for key, value in entries.items() if value}
    plan = _Plan(empty=tuple(sorted(key for key, value in entries.items() if not value)))
    declared = _plan_keys(registry, declared_fields)
    await _classify_declared(plan, values, declared, reader)
    for provider, roles in sorted(_app_candidates(registry, legacy_apps, declared).items()):
        present = {role: {key for key in keys if key in values} for role, keys in roles.items()}
        if present["id"] or present["secret"]:
            await _classify_app(plan, provider, present, values, apps)
    accounted = set(declared) | plan.app_keys | set(plan.unresolved)
    for key in values:
        if key not in accounted:
            plan.unresolved[key] = UNDECLARED
    return plan


def _report(
    plan: _Plan, env_path: Path, *, dry_run: bool = False, deleted: bool = False
) -> MigrationReport:
    written = {**plan.moves, **plan.already}
    return MigrationReport(
        migrated=tuple(sorted(str(ref) for ref in plan.moves.values())),
        already=tuple(sorted(str(ref) for ref in plan.already.values())),
        apps=tuple(sorted(plan.apps)),
        app_keys=tuple(sorted(plan.app_keys)),
        unresolved=tuple(sorted(plan.unresolved.items())),
        empty=plan.empty,
        connections=tuple(sorted({ref.connection for ref in written.values()})),
        path=str(env_path),
        dry_run=dry_run,
        deleted=deleted,
    )


async def migrate_connector_secrets(
    *,
    env_path: Path,
    registry: ConnectionRegistry,
    declared_fields: DeclaredFields,
    secret_store: SecretStore | None,
    verify_store: Callable[[], SecretStore] | None,
    actor_did: str,
    sink: AuditSink,
    legacy_apps: LegacyApps | None = None,
    app_store: OAuthAppStore | None = None,
    dry_run: bool = False,
    drop_undeclared: bool = False,
) -> MigrationReport:
    """Move ``env_path`` into sealed custody and app slots, verify, delete, audit.

    Raises:
        ExtensionError: ``MIGRATION_UNDECLARED_KEYS`` when a non-empty value would
            be lost and ``drop_undeclared`` is not set (nothing written, file kept);
            ``MIGRATION_VERIFY_FAILED`` when a value read back differs (file kept);
            or the store's own refusal (no cipher, a symlinked or loose file).
    """
    if not os.path.lexists(env_path):
        return MigrationReport(skipped=True, path=str(env_path))
    # EnvFile keeps the O_NOFOLLOW + owner + mode checks: a symlinked or
    # world-readable file is refused, never read.
    entries = await EnvFile(env_path).read()
    plan = await _plan(
        entries,
        registry=registry,
        declared_fields=declared_fields,
        legacy_apps=legacy_apps or (lambda _instance, _extension: None),
        reader=secret_store,
        apps=app_store,
    )
    if dry_run:
        return _report(plan, env_path, dry_run=True)
    if secret_store is None or verify_store is None or (plan.apps and app_store is None):
        raise ExtensionError(
            code="SECRET_STORE_UNCONFIGURED",
            message="no credential custody is available to migrate connections.env into",
            details={"path": str(env_path)},
        )
    if plan.unresolved and not drop_undeclared:
        raise _refuse(plan, env_path, actor_did, sink)
    for key, ref in plan.moves.items():
        await secret_store.put(ref, entries[key])
    await _verify(plan.moves, entries, verify_store())
    if app_store is not None:
        await _move_apps(plan, app_store, actor_did, sink)
    _audit_drops(plan, actor_did, sink)
    _delete(env_path)
    report = replace(
        _report(plan, env_path, deleted=True),
        unresolved=(),
        dropped=tuple(sorted(plan.unresolved)),
    )
    emit(
        AuditEvent(
            actor_did=actor_did,
            action="connection.credential.migrated",
            target="secret:connections.env",
            outcome="allow",
            extra={
                "count": len(plan.moves),
                "already": len(plan.already),
                "apps": list(report.apps),
                "dropped": list(report.dropped),
                "connections": list(report.connections),
            },
        ),
        sink,
    )
    return report


def _refuse(plan: _Plan, env_path: Path, actor_did: str, sink: AuditSink) -> ExtensionError:
    """Audit the refusal and build the error naming every key that would be lost."""
    keys = sorted(plan.unresolved)
    emit(
        AuditEvent(
            actor_did=actor_did,
            action="connection.credential.migration_refused",
            target="secret:connections.env",
            outcome="deny",
            extra={"unresolved": [[key, plan.unresolved[key]] for key in keys]},
        ),
        sink,
    )
    listing = ", ".join(f"{key} ({plan.unresolved[key]})" for key in keys)
    return ExtensionError(
        code=MIGRATION_UNDECLARED_KEYS,
        message=(
            f"{env_path} holds {len(keys)} value(s) Arc cannot move: {listing}. Nothing was "
            "moved and the file was kept. Review with `arc connector migrate-secrets "
            "--dry-run`, re-enter any you still need (for example `arc connector oauth-app "
            "<provider>`), then run `arc connector migrate-secrets --drop-undeclared` to "
            "drop the rest on purpose."
        ),
        details={"keys": keys, "path": str(env_path)},
    )


async def _move_apps(
    plan: _Plan, app_store: OAuthAppStore, actor_did: str, sink: AuditSink
) -> None:
    """Write each provider's app slot, read it back, audit the mapping."""
    for provider, (client_id, client_secret) in sorted(plan.apps.items()):
        await app_store.put(
            provider, client_id=client_id, client_secret=client_secret, actor_did=actor_did
        )
        stored = await app_store.get(provider)
        if stored is None or not _same_app(
            stored.client_id, stored.client_secret.reveal(), {client_id}, {client_secret}
        ):
            raise ExtensionError(
                code="MIGRATION_VERIFY_FAILED",
                message=(
                    f"the {provider} sign-in app did not read back as written; "
                    "connections.env was kept"
                ),
                details={"provider": provider},
            )
        emit(
            AuditEvent(
                actor_did=actor_did,
                action="connection.credential.app_migrated",
                target=f"oauth_app:{provider}",
                outcome="allow",
                extra={"provider": provider},
            ),
            sink,
        )


def _audit_drops(plan: _Plan, actor_did: str, sink: AuditSink) -> None:
    """One audit event per value dropped on purpose: its key and why, never the value."""
    for key in sorted(plan.unresolved):
        emit(
            AuditEvent(
                actor_did=actor_did,
                action="connection.credential.dropped",
                target="secret:connections.env",
                outcome="allow",
                extra={"key": key, "reason": plan.unresolved[key]},
            ),
            sink,
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
    #: OAuth app slots (providers) moved, or that a dry run would move (P18-3).
    apps: tuple[str, ...] = ()


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
    "MIGRATION_UNDECLARED_KEYS",
    "MIGRATOR_DID",
    "DeclaredFields",
    "LegacyApp",
    "LegacyApps",
    "MigrationReport",
    "ResealReport",
    "legacy_env_path",
    "migrate_connector_secrets",
    "reseal_connector_secrets",
]
