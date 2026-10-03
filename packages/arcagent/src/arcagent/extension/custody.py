"""P18-2 — sealed custody rows for connector credentials.

Every connector credential Arc holds lives in ONE arcstore row per connection,
``mutable_records/connector_credentials/<connection>``. Every value in the row is
sealed by a :class:`CredentialCipher` whose associated data binds it to its exact
``(connection, field)`` coordinate, so a database reader learns nothing and a
ciphertext moved to another connection or field fails to open.

The row also carries the short-lived access token and a fenced refresh lease, so
one compare-and-set commits a rotated refresh token, the new access token, its
expiry and the lease release together. There is no window in which the value and
its metadata disagree, and no file lock: the CAS works across processes and hosts
on Postgres and on the in-memory fake alike.

Two rules keep the CAS honest:

* Every CAS'd key (``revision``, ``lease``, ``access``, ``fields``) is always
  present, null when empty; arcstore's ``where`` cannot match a missing key.
* ``update_if`` is a shallow merge, so a write of ``fields`` writes the WHOLE map
  taken from the row it read. The ``revision`` CAS guarantees no concurrent change
  is lost.

This module is the only one that touches the collection.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from arctrust.audit import AuditEvent, AuditSink, emit
from arctrust.connector_cipher import CredentialCustodyUnavailableError, CredentialSealError
from pydantic import BaseModel, ConfigDict, Field

from arcagent.core.errors import ExtensionError
from arcagent.extension.coordinates import is_coordinate
from arcagent.extension.secrets import Secret, SecretRef
from arcagent.extension.state import MutableConnectionBackend

#: The arcstore collection holding one sealed row per connection.
CREDENTIAL_COLLECTION = "connector_credentials"

#: The slot an access token is sealed under (never a declared field name).
ACCESS_SLOT = "access_token"

#: Actor recorded on the backend's own mutation events for custody bookkeeping.
CUSTODY_DID = "did:arc:system:credential-custody"

_CAS_ATTEMPTS = 8

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def parse_time(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class CredentialCipher(Protocol):
    """Seals and opens one value bound to ``(scope, slot)``. No key accessor."""

    @property
    def kind(self) -> str: ...

    def seal(self, plaintext: bytes, *, scope: str, slot: str) -> str: ...

    def open(self, sealed: str, *, scope: str, slot: str) -> bytes: ...


class SealedField(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sealed: str
    updated_at: str


class SealedAccess(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sealed: str
    issued_at: str
    expires_at: str
    #: The granted scope string. Not secret.
    scope: str | None = None


class LeaseRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    owner: str
    fence: int
    expires_at: str


class CredentialRow(BaseModel):
    """One connection's custody row, exactly as stored (values sealed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connection: str
    revision: int = 0
    #: +1 when an operator-supplied field or the refresh token changes; NOT on an
    #: access refresh. Read by the health authority as ``credential_generation``.
    generation: int = 0
    fields: dict[str, SealedField] = Field(default_factory=dict)
    access: SealedAccess | None = None
    lease: LeaseRecord | None = None
    fence_counter: int = 0
    cipher: str
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class RefreshLease:
    """The right to renew one connection's access token, fenced and time-bound."""

    connection: str
    owner: str
    fence: int
    expires_at: datetime


@dataclass(frozen=True)
class AccessToken:
    """An opened access token and its lifetime."""

    token: Secret
    issued_at: datetime
    expires_at: datetime
    scope: str | None = None


def unreadable(connection: str, reason: str) -> ExtensionError:
    """The refusal for a row this process cannot open. Names no material."""
    return ExtensionError(
        code="CREDENTIAL_UNREADABLE",
        message=f"the stored credential for {connection!r} could not be opened: {reason}",
        details={"connection": connection},
    )


def custody_unavailable(connection: str) -> ExtensionError:
    """The refusal when the custody transit cannot answer (P18-2F). Fail closed.

    Distinct from :func:`unreadable`: the stored value is fine, the vault holding
    its key is not reachable, so the operator must NOT reconnect (that would
    replace a good credential); the card says "wait".
    """
    return ExtensionError(
        code="CREDENTIAL_CUSTODY_UNAVAILABLE",
        message=(
            f"the credential vault is not answering, so the credential for {connection!r} "
            "is locked until it is back; nothing was read or written"
        ),
        details={"connection": connection, "retryable": True},
    )


def _busy(connection: str) -> ExtensionError:
    return ExtensionError(
        code="CREDENTIAL_STORE_BUSY",
        message=f"the credential for {connection!r} kept changing while it was written",
        details={"connection": connection},
    )


def _check_coordinate(kind: str, value: str) -> None:
    if not is_coordinate(value):
        raise ExtensionError(
            code="SECRET_REF_INVALID",
            message=f"{kind} is not a valid credential coordinate",
            details={"coordinate": kind},
        )


class CredentialRowStore:
    """CAS primitives over ``connector_credentials``. The only module that touches it."""

    def __init__(
        self,
        backend: MutableConnectionBackend,
        cipher: CredentialCipher,
        *,
        sink: AuditSink | None = None,
        clock: Clock = _utcnow,
    ) -> None:
        self._backend = backend
        self._cipher = cipher
        self._sink = sink
        self._clock = clock

    @property
    def cipher_kind(self) -> str:
        return self._cipher.kind

    def now(self) -> datetime:
        return self._clock()

    # --- reading -----------------------------------------------------------

    async def read(self, connection: str) -> CredentialRow | None:
        raw = await self._backend.mutable_read(CREDENTIAL_COLLECTION, connection)
        return None if raw is None else CredentialRow.model_validate(raw)

    async def connections(self) -> list[str]:
        """Every connection with a custody row (what the proactive renewer scans)."""
        rows = await self._backend.mutable_query(CREDENTIAL_COLLECTION)
        return sorted(str(row["connection"]) for row in rows if "connection" in row)

    async def generation(self, connection: str) -> int | None:
        row = await self.read(connection)
        return None if row is None else row.generation

    async def open_field(self, row: CredentialRow, name: str) -> Secret | None:
        """One field's value, or ``None`` when the row does not hold it.

        Raises:
            ExtensionError: ``CREDENTIAL_UNREADABLE`` for a row sealed by another
                cipher or a value that does not open for this coordinate;
                ``CREDENTIAL_CUSTODY_UNAVAILABLE`` when the custody transit
                cannot answer.
        """
        sealed = row.fields.get(name)
        if sealed is None:
            return None
        return Secret(await self._open(row, sealed.sealed, name))

    async def open_access(self, row: CredentialRow) -> AccessToken | None:
        access = row.access
        if access is None:
            return None
        return AccessToken(
            token=Secret(await self._open(row, access.sealed, ACCESS_SLOT)),
            issued_at=parse_time(access.issued_at),
            expires_at=parse_time(access.expires_at),
            scope=access.scope,
        )

    async def _open(self, row: CredentialRow, sealed: str, slot: str) -> str:
        self._require_cipher(row)
        # Off the event loop: a transit cipher is a blocking round trip (P18-2F).
        try:
            raw = await asyncio.to_thread(
                self._cipher.open, sealed, scope=row.connection, slot=slot
            )
            return raw.decode("utf-8")
        except CredentialCustodyUnavailableError:
            raise custody_unavailable(row.connection) from None
        except (CredentialSealError, UnicodeDecodeError):
            raise unreadable(
                row.connection, "it does not open under this deployment's key"
            ) from None

    async def _seal(self, connection: str, slot: str, value: str) -> str:
        try:
            return await asyncio.to_thread(
                self._cipher.seal, value.encode("utf-8"), scope=connection, slot=slot
            )
        except CredentialCustodyUnavailableError:
            raise custody_unavailable(connection) from None
        except CredentialSealError as exc:
            raise ExtensionError(
                code="SECRET_VALUE_INVALID",
                message=f"the value for {connection}/{slot} could not be sealed: {exc}",
                details={"secret": f"{connection}/{slot}"},
            ) from None

    # --- operator-supplied fields -----------------------------------------

    async def put_fields(
        self,
        connection: str,
        values: Mapping[str, str],
        *,
        actor_did: str,
        bump_generation: bool = True,
    ) -> int:
        """Seal and store ``values``, keeping every other field. Returns the generation."""
        _check_coordinate("connection", connection)
        for name in values:
            _check_coordinate("field", name)
        stamp = _iso(self._clock())
        sealed = {
            name: SealedField(sealed=await self._seal(connection, name, value), updated_at=stamp)
            for name, value in values.items()
        }
        for _ in range(_CAS_ATTEMPTS):
            row = await self._read_or_create(connection, actor_did)
            self._require_cipher(row)
            fields = {**row.fields, **sealed}
            generation = row.generation + (1 if bump_generation else 0)
            patch = {
                "fields": {name: value.model_dump() for name, value in fields.items()},
                "generation": generation,
            }
            if await self._cas(row, patch, actor_did=actor_did):
                return generation
        raise _busy(connection)

    async def put_grant(
        self,
        connection: str,
        *,
        refresh_field: str,
        refresh_token: str,
        access_token: str,
        issued_at: datetime,
        expires_at: datetime,
        scope: str | None,
        actor_did: str,
    ) -> int:
        """Store a fresh OAuth grant (refresh + access) in ONE write. Returns the generation.

        Used right after an authorization-code exchange, so the access token the
        exchange already issued is used instead of spending a refresh at once.
        """
        _check_coordinate("connection", connection)
        _check_coordinate("field", refresh_field)
        sealed_refresh = await self._seal(connection, refresh_field, refresh_token)
        sealed_access = await self._seal(connection, ACCESS_SLOT, access_token)
        for _ in range(_CAS_ATTEMPTS):
            row = await self._read_or_create(connection, actor_did)
            self._require_cipher(row)
            fields = {name: value.model_dump() for name, value in row.fields.items()}
            fields[refresh_field] = {"sealed": sealed_refresh, "updated_at": _iso(issued_at)}
            generation = row.generation + 1
            patch = {
                "fields": fields,
                "access": {
                    "sealed": sealed_access,
                    "issued_at": _iso(issued_at),
                    "expires_at": _iso(expires_at),
                    "scope": scope,
                },
                "generation": generation,
            }
            if await self._cas(row, patch, actor_did=actor_did):
                return generation
        raise _busy(connection)

    async def delete_fields(
        self, connection: str, names: Sequence[str], *, actor_did: str
    ) -> tuple[str, ...]:
        """Drop the named fields. Returns the names that were actually present."""
        for _ in range(_CAS_ATTEMPTS):
            row = await self.read(connection)
            if row is None:
                return ()
            removed = tuple(name for name in names if name in row.fields)
            if not removed:
                return ()
            fields = {
                name: value.model_dump()
                for name, value in row.fields.items()
                if name not in removed
            }
            patch = {"fields": fields, "generation": row.generation + 1}
            if await self._cas(row, patch, actor_did=actor_did):
                return removed
        raise _busy(connection)

    async def forget(self, connection: str, *, actor_did: str) -> bool:
        """Delete the whole row, so no undeclared leftover survives a removal."""
        return await self._backend.mutable_delete(
            CREDENTIAL_COLLECTION, connection, actor_did=actor_did, sink=self._sink
        )

    # --- the refresh lease -------------------------------------------------

    async def acquire_lease(
        self, connection: str, owner: str, ttl: timedelta
    ) -> RefreshLease | None:
        """Take the single-refresher lease, or ``None`` while another holder owns it.

        A lease whose expiry lies further ahead than ``2 * ttl`` was not written by
        a well-behaved holder (every holder writes ``now + ttl``); it is treated as
        void so a forged far-future lease cannot block renewal forever.
        """
        for _ in range(_CAS_ATTEMPTS):
            row = await self.read(connection)
            if row is None:
                return None
            now = self._clock()
            held = row.lease
            hogged = False
            if held is not None and held.owner != owner:
                expires = parse_time(held.expires_at)
                hogged = expires > now + 2 * ttl
                if expires > now and not hogged:
                    return None
            fence = row.fence_counter + 1
            lease = RefreshLease(connection, owner, fence, now + ttl)
            patch = {
                "lease": {"owner": owner, "fence": fence, "expires_at": _iso(lease.expires_at)},
                "fence_counter": fence,
            }
            if await self._cas(row, patch, actor_did=CUSTODY_DID):
                if hogged:
                    self._audit_contended(connection, reason="lease_hogged")
                return lease
        return None

    async def renew_lease(self, lease: RefreshLease, ttl: timedelta) -> bool:
        """Extend a lease this holder still owns. False when it was lost."""
        row = await self.read(lease.connection)
        if row is None or not _holds(row, lease):
            return False
        expires = _iso(self._clock() + ttl)
        patch = {"lease": {"owner": lease.owner, "fence": lease.fence, "expires_at": expires}}
        return await self._cas(row, patch, actor_did=CUSTODY_DID, lease=lease)

    async def release_lease(self, lease: RefreshLease) -> None:
        """Give the lease back. A no-op when a commit already cleared it."""
        for _ in range(_CAS_ATTEMPTS):
            row = await self.read(lease.connection)
            if row is None or not _holds(row, lease):
                return
            if await self._cas(row, {"lease": None}, actor_did=CUSTODY_DID, lease=lease):
                return

    async def commit_renewal(
        self,
        lease: RefreshLease,
        *,
        access_token: str,
        issued_at: datetime,
        expires_at: datetime,
        scope: str | None,
        rotated_refresh: str | None,
        refresh_field: str,
        expected_generation: int,
        actor_did: str = CUSTODY_DID,
    ) -> bool:
        """Commit a renewal in ONE ``update_if``: access, rotated refresh, lease release.

        ``expected_generation`` is the generation of the row the refresh token was
        read from. If an operator reconnected meanwhile (new refresh token, new
        generation), this renewal was made with a superseded credential and is
        refused, so it can never overwrite the operator's new one.

        Re-reads immediately before the write (no await between that read and the
        CAS except the CAS itself) and builds the patch from it, so a field an
        operator changed during the provider call is kept. Returns False, writing
        nothing, when this holder no longer owns the lease (stalled past its TTL,
        or a replayed commit from an older lease).
        """
        connection = lease.connection
        sealed_access = await self._seal(connection, ACCESS_SLOT, access_token)
        sealed_refresh = (
            await self._seal(connection, refresh_field, rotated_refresh)
            if rotated_refresh is not None
            else None
        )
        row = await self.read(connection)
        if row is None or not _holds(row, lease) or row.generation != expected_generation:
            return False
        self._require_cipher(row)
        fields = {name: value.model_dump() for name, value in row.fields.items()}
        if sealed_refresh is not None:
            fields[refresh_field] = {"sealed": sealed_refresh, "updated_at": _iso(issued_at)}
        patch: dict[str, Any] = {
            "access": {
                "sealed": sealed_access,
                "issued_at": _iso(issued_at),
                "expires_at": _iso(expires_at),
                "scope": scope,
            },
            "fields": fields,
            "generation": row.generation + (1 if sealed_refresh is not None else 0),
            "lease": None,
        }
        return await self._cas(
            row, patch, actor_did=actor_did, lease=lease, generation=expected_generation
        )

    # --- internals ---------------------------------------------------------

    async def _read_or_create(self, connection: str, actor_did: str) -> CredentialRow:
        row = await self.read(connection)
        if row is not None:
            return row
        stamp = _iso(self._clock())
        fresh = CredentialRow(
            connection=connection, cipher=self._cipher.kind, created_at=stamp, updated_at=stamp
        )
        created = await self._backend.mutable_create_batch(
            CREDENTIAL_COLLECTION,
            [(connection, fresh.model_dump(mode="json"))],
            actor_did=actor_did,
            sink=self._sink,
        )
        return CredentialRow.model_validate(created[0])

    def _require_cipher(self, row: CredentialRow) -> None:
        if row.cipher != self._cipher.kind:
            raise unreadable(row.connection, _cipher_mismatch(row.cipher, self._cipher.kind))

    async def _cas(
        self,
        row: CredentialRow,
        patch: dict[str, Any],
        *,
        actor_did: str,
        lease: RefreshLease | None = None,
        generation: int | None = None,
    ) -> bool:
        where: dict[str, Any] = {"revision": row.revision}
        if generation is not None:
            where["generation"] = generation
        if lease is not None:
            where["lease.owner"] = lease.owner
            where["lease.fence"] = lease.fence
        full = {**patch, "revision": row.revision + 1, "updated_at": _iso(self._clock())}
        return await self._backend.update_if(
            CREDENTIAL_COLLECTION,
            row.connection,
            full,
            where,
            actor_did=actor_did,
            sink=self._sink,
        )

    def _audit_contended(self, connection: str, *, reason: str) -> None:
        if self._sink is None:
            return
        emit(
            AuditEvent(
                actor_did=CUSTODY_DID,
                action="connection.credential.lease_contended",
                target=f"connection:{connection}",
                outcome="allow",
                extra={"reason": reason},
            ),
            self._sink,
        )


def _cipher_mismatch(sealed_by: str, current: str) -> str:
    if sealed_by == "xc1" and current == "transit1":
        return (
            "it is still sealed under the in-process key; run "
            "`arc connector migrate-secrets --reseal` to move it into the vault"
        )
    return "it was sealed by a different cipher"


def _holds(row: CredentialRow, lease: RefreshLease) -> bool:
    held = row.lease
    return held is not None and held.owner == lease.owner and held.fence == lease.fence


class SealedCredentialBackend:
    """One ``(connection, field)`` at a time through :class:`CredentialRowStore`.

    Implements :class:`~arcagent.extension.secrets.SecretBackend`; ``put`` and
    ``delete`` are operator changes and bump the row's generation.
    """

    store_name = "sealed"

    def __init__(self, rows: CredentialRowStore, *, actor_did: str = CUSTODY_DID) -> None:
        self._rows = rows
        self._actor_did = actor_did

    @property
    def rows(self) -> CredentialRowStore:
        return self._rows

    async def get(self, ref: SecretRef) -> str | None:
        row = await self._rows.read(ref.connection)
        if row is None:
            return None
        found = await self._rows.open_field(row, ref.field)
        return None if found is None else found.reveal()

    async def put(self, ref: SecretRef, value: str) -> None:
        await self._rows.put_fields(ref.connection, {ref.field: value}, actor_did=self._actor_did)

    async def present(self, connection: str) -> frozenset[str]:
        row = await self._rows.read(connection)
        return frozenset(row.fields) if row is not None else frozenset()

    async def delete(self, ref: SecretRef) -> bool:
        removed = await self._rows.delete_fields(
            ref.connection, [ref.field], actor_did=self._actor_did
        )
        return bool(removed)


__all__ = [
    "ACCESS_SLOT",
    "CREDENTIAL_COLLECTION",
    "CUSTODY_DID",
    "AccessToken",
    "CredentialCipher",
    "CredentialRow",
    "CredentialRowStore",
    "RefreshLease",
    "SealedCredentialBackend",
    "parse_time",
    "unreadable",
]
