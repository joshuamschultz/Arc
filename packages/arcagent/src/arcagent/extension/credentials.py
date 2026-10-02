"""SPEC-062 COMP-011 / P18-2 — keeping a connected account alive without a human.

Renewal is driven by the clock, not by a failure: an access token is refreshed once
three quarters of its lifetime has elapsed (REQ-287). Waiting for a call to return
401 means every connection breaks at least once per token lifetime, in the middle
of whatever the agent was doing.

The hard part is REQ-288, and it is not obvious. Any provider that rotates its
refresh token issues that token *single-use*: the moment one is exchanged it is
dead. So two renewals racing for the same account do not merely duplicate work,
they destroy the account: the second presents a token the authorization server
has already consumed, gets ``invalid_grant``, and nothing recovers that except a
human re-consenting.

Three things together prevent it, and all three are needed:

1. **One refresher per account, across processes.** A fenced lease in the custody
   row (:mod:`arcagent.extension.custody`), taken by compare-and-set. An
   in-process lock in front of it keeps one process from polling its own lease.
2. **A re-read under the lease.** The lease alone only serialises renewals; the
   waiter would still exchange the now-consumed token. Under the lease the row is
   read again, and a token another holder already renewed is simply used.
3. **Persist before use, in one write.** The rotated refresh token, the new access
   token, its expiry and the lease release are committed by ONE ``update_if``.
   Nothing is returned to a caller until that commit landed; a commit that loses
   its lease fails honestly and is never retried with the consumed token.

Failure classification is the other half (REQ-289). ``invalid_grant`` and the
consent codes mean a human must act, so retrying is not merely useless, it burns
the connection further. Those stop immediately and are reported to the connection
health authority, which marks the connection and sends the operator one notice
per outage. Once a connection is ``needs_you`` for a dead credential, no provider
call is made again until the credential generation changes (the operator
reconnected).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from arctrust.audit import AuditEvent, AuditSink, emit

from arcagent.core.errors import ExtensionError
from arcagent.extension.connection_health import (
    AUTH_REASONS,
    HealthReporter,
    HealthSignal,
    classify,
)
from arcagent.extension.custody import CredentialRow, CredentialRowStore, RefreshLease
from arcagent.extension.manifest import OAuthFlow
from arcagent.extension.secrets import Secret
from arcagent.extension.state import ConnectionStateStore

_logger = logging.getLogger("arcagent.extension.credentials")

#: OAuth error codes that mean a human must act. Retrying one of these is not just
#: futile, it consumes attempts against a connection that is already broken.
TERMINAL_ERROR_CODES = frozenset(
    {
        "invalid_grant",
        "consent_required",
        "interaction_required",
        "auth_required",
        "credential_missing",
        "credential_unreadable",
    }
)

#: Actor recorded when the renewal path itself acts (no agent asked for it).
ESCALATION_DID = "did:arc:arcagent:credential-lifecycle"

#: The proactive renewer that keeps every connection's access token fresh.
RENEWER_DID = "did:arc:system:credential-renewer"

#: Fraction of an access token's lifetime that may elapse before renewal is due.
RENEWAL_FRACTION = 0.75

#: A token this close to expiry is always due, whatever its lifetime.
EXPIRY_FLOOR = timedelta(seconds=120)

#: Lifetime assumed when a provider answers without ``expires_in``.
DEFAULT_LIFETIME = timedelta(hours=1)

_LEASE_POLL_SECONDS = 0.25
_PROVIDER_ATTEMPTS = 3
_BACKOFF_SECONDS = (0.5, 1.0)
_MAX_RETRY_AFTER_SECONDS = 5.0


class CredentialRenewalError(ExtensionError):
    """A renewal attempt failed. ``terminal`` decides whether retrying is sane."""

    def __init__(self, *, error_code: str, message: str, retry_after: float | None = None) -> None:
        super().__init__(
            code="CREDENTIAL_RENEWAL_FAILED",
            message=message,
            details={"error_code": error_code},
        )
        self.error_code = error_code
        self.retry_after = retry_after

    @property
    def terminal(self) -> bool:
        """True when only a human can fix this."""
        return self.error_code in TERMINAL_ERROR_CODES


@dataclass(frozen=True)
class RenewedCredential:
    """What one refresh exchange with the authorization server produced."""

    access_token: Secret
    expires_in: int
    #: Set only when the provider rotated the refresh token.
    refresh_token: Secret | None = None
    scope: str | None = None


@dataclass(frozen=True)
class RefreshRequest:
    """Everything one provider refresh call needs. Values stay wrapped."""

    flow: OAuthFlow
    refresh_token: Secret
    client_id: str
    client_secret: Secret


RefreshFn = Callable[[RefreshRequest], Awaitable[RenewedCredential]]
Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


class ClientCredentialSource(Protocol):
    """Where a connection's OAuth client id/secret come from.

    P18-2 reads them from the connection's own custody fields; P18-3 swaps in the
    deployment's OAuth app slot.
    """

    def __call__(
        self, rows: CredentialRowStore, row: CredentialRow, flow: OAuthFlow
    ) -> tuple[str, Secret] | None: ...


def connection_client(
    rows: CredentialRowStore, row: CredentialRow, flow: OAuthFlow
) -> tuple[str, Secret] | None:
    """The client id/secret stored on the connection itself (Dropbox app key/secret)."""
    client_id = rows.open_field(row, flow.client_id_secret)
    client_secret = rows.open_field(row, flow.client_secret_secret)
    if client_id is None or client_secret is None:
        return None
    return client_id.reveal(), client_secret


def _utcnow() -> datetime:
    return datetime.now(UTC)


def is_due(row: CredentialRow, *, now: datetime) -> bool:
    """True when the row's access token should be renewed now."""
    access = row.access
    if access is None:
        return True
    issued = datetime.fromisoformat(access.issued_at)
    expires = datetime.fromisoformat(access.expires_at)
    if expires - now <= EXPIRY_FLOOR:
        return True
    return now >= issued + (expires - issued) * RENEWAL_FRACTION


def _missing(connection: str, what: str) -> CredentialRenewalError:
    return CredentialRenewalError(
        error_code="credential_missing",
        message=f"{connection!r} has no stored {what}; connect it again",
    )


@dataclass
class RenewalPlanner:
    """Single-refresher, persist-before-use renewal of OAuth access tokens.

    ``health`` has no default on purpose: a planner wired without a health path
    would discover terminal failures and have nowhere to report them.
    """

    rows: CredentialRowStore
    refresh: RefreshFn
    health: HealthReporter
    owner_id: str
    state: ConnectionStateStore | None = None
    client: ClientCredentialSource = connection_client
    sink: AuditSink | None = None
    actor_did: str = RENEWER_DID
    clock: Clock = _utcnow
    sleep: Sleep = asyncio.sleep
    lease_ttl: timedelta = timedelta(seconds=60)
    provider_timeout: float = 20.0
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False, repr=False)

    def is_due(self, row: CredentialRow, *, now: datetime) -> bool:
        return is_due(row, now=now)

    async def ensure_fresh(self, connection: str, *, flow: OAuthFlow, force: bool = False) -> bool:
        """Renew ``connection``'s access token if it is due (or ``force``d by a 401).

        Returns True when THIS call committed a renewal, False when nothing was
        needed or another holder renewed it meanwhile. Raises
        :class:`CredentialRenewalError` when a renewal was needed and could not be
        completed; a terminal one has already been reported to the health
        authority.
        """
        row = await self.rows.read(connection)
        if row is None or (row.access is None and flow.refresh_token_secret not in row.fields):
            await self._report(connection, "credential_missing", "not connected yet", row)
            raise _missing(connection, "credential")
        await self._refuse_dead_credential(connection, row)
        if not force and not is_due(row, now=self.clock()):
            return False
        async with self._locks.setdefault(connection, asyncio.Lock()):
            lease = await self._wait_for_lease(connection, force=force)
            if lease is None:
                return False
            try:
                return await self._renew_under_lease(lease, flow, force=force)
            finally:
                await self.rows.release_lease(lease)

    async def _refuse_dead_credential(self, connection: str, row: CredentialRow) -> None:
        """Zero provider calls while ``needs_you`` until the credential generation changes."""
        if self.state is None:
            return
        record = await self.state.get(connection)
        if (
            record is not None
            and record.status == "needs_you"
            and record.reason_code in AUTH_REASONS
            and record.credential_generation is not None
            and record.credential_generation == row.generation
        ):
            raise CredentialRenewalError(
                error_code=record.reason_code or "auth_required",
                message=(
                    f"{connection!r} needs to be reconnected; its credential was rejected and "
                    "has not changed since"
                ),
            )

    async def _wait_for_lease(self, connection: str, *, force: bool) -> RefreshLease | None:
        """Take the cross-process lease, or return None if a peer renewed meanwhile."""
        polls = max(1, int(self.lease_ttl.total_seconds() / _LEASE_POLL_SECONDS))
        for _ in range(polls):
            lease = await self.rows.acquire_lease(connection, self.owner_id, self.lease_ttl)
            if lease is not None:
                return lease
            await self.sleep(_LEASE_POLL_SECONDS)
            row = await self.rows.read(connection)
            if row is None:
                raise _missing(connection, "credential")
            if not force and not is_due(row, now=self.clock()):
                return None
        self._audit(
            "connection.credential.lease_contended",
            connection,
            "deny",
            {"waited_ms": int(self.lease_ttl.total_seconds() * 1000)},
        )
        await self._report(connection, "provider_unavailable", "credential renewal busy", None)
        raise CredentialRenewalError(
            error_code="renewal_busy",
            message=f"another process kept renewing {connection!r}; try again",
        )

    async def _renew_under_lease(
        self, lease: RefreshLease, flow: OAuthFlow, *, force: bool
    ) -> bool:
        connection = lease.connection
        row = await self.rows.read(connection)  # re-read UNDER the lease
        if row is None:
            raise _missing(connection, "credential")
        if not force and not is_due(row, now=self.clock()):
            return False
        request = await self._request(row, flow)
        if not await self.rows.renew_lease(lease, self.lease_ttl):
            raise CredentialRenewalError(
                error_code="lease_lost", message=f"lost the renewal lease for {connection!r}"
            )
        renewed = await self._call_provider(connection, request, row)
        now = self.clock()
        lifetime = (
            renewed.expires_in if renewed.expires_in > 0 else int(DEFAULT_LIFETIME.total_seconds())
        )
        expires_at = now + timedelta(seconds=lifetime)
        rotated = renewed.refresh_token.reveal() if renewed.refresh_token is not None else None
        committed = await self.rows.commit_renewal(
            lease,
            access_token=renewed.access_token.reveal(),
            issued_at=now,
            expires_at=expires_at,
            scope=renewed.scope,
            rotated_refresh=rotated,
            refresh_field=flow.refresh_token_secret,
            actor_did=self.actor_did,
        )
        if not committed:
            self._audit(
                "connection.credential.renew_failed",
                connection,
                "error",
                {"error_code": "renewal_commit_lost"},
            )
            raise CredentialRenewalError(
                error_code="renewal_commit_lost",
                message=(
                    f"the renewal of {connection!r} lost its lease before it could be stored; "
                    "it was not used"
                ),
            )
        generation = row.generation + (1 if rotated is not None else 0)
        await self._mirror(connection, expires_at, now, flow)
        self._audit(
            "connection.credential.renewed",
            connection,
            "allow",
            {"rotated": rotated is not None, "expires_in": lifetime, "generation": generation},
        )
        await self.health.report(
            connection,
            HealthSignal(
                ok=True,
                source="credential",
                checked_by=self.actor_did,
                credential_generation=generation,
            ),
        )
        return True

    async def _request(self, row: CredentialRow, flow: OAuthFlow) -> RefreshRequest:
        connection = row.connection
        try:
            refresh = self.rows.open_field(row, flow.refresh_token_secret)
            client = self.client(self.rows, row, flow)
        except ExtensionError as exc:
            if exc.code != "CREDENTIAL_UNREADABLE":
                raise
            await self._report(connection, "credential_unreadable", exc.message, row)
            raise CredentialRenewalError(
                error_code="credential_unreadable", message=exc.message
            ) from exc
        if refresh is None:
            await self._report(connection, "credential_missing", "no refresh token", row)
            raise _missing(connection, "refresh token")
        if client is None:
            await self._report(connection, "credential_missing", "no OAuth client", row)
            raise _missing(connection, "OAuth client id/secret")
        client_id, client_secret = client
        return RefreshRequest(
            flow=flow, refresh_token=refresh, client_id=client_id, client_secret=client_secret
        )

    async def _call_provider(
        self, connection: str, request: RefreshRequest, row: CredentialRow
    ) -> RenewedCredential:
        """At most three attempts, all inside one ``provider_timeout`` budget."""
        last: CredentialRenewalError | None = None
        try:
            async with asyncio.timeout(self.provider_timeout):
                for attempt in range(_PROVIDER_ATTEMPTS):
                    try:
                        return await self.refresh(request)
                    except CredentialRenewalError as exc:
                        if exc.terminal:
                            await self._fail(connection, exc.error_code, exc.message, row)
                            raise
                        last = exc
                    if attempt < _PROVIDER_ATTEMPTS - 1:
                        await self.sleep(self._backoff(attempt, last))
        except TimeoutError:
            last = CredentialRenewalError(
                error_code="provider_unavailable",
                message=f"the provider did not answer within {self.provider_timeout:g} s",
            )
        detail = last.message if last is not None else "renewal failed"
        code = last.error_code if last is not None else "provider_unavailable"
        await self._fail(connection, code, detail, row)
        raise CredentialRenewalError(error_code="provider_unavailable", message=detail)

    @staticmethod
    def _backoff(attempt: int, last: CredentialRenewalError | None) -> float:
        hinted = last.retry_after if last is not None else None
        if hinted is not None and 0 <= hinted <= _MAX_RETRY_AFTER_SECONDS:
            return hinted
        return _BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)]

    async def _fail(self, connection: str, code: str, detail: str, row: CredentialRow) -> None:
        await self._report(connection, code, detail, row)
        self._audit("connection.credential.renew_failed", connection, "deny", {"error_code": code})

    async def _mirror(
        self, connection: str, expires_at: datetime, now: datetime, flow: OAuthFlow
    ) -> None:
        """Write the display mirror. Decisions never read it, so a failure is only logged."""
        if self.state is None:
            return
        try:
            await self.state.record_credential_metadata(
                connection,
                expires_at=expires_at.isoformat(),
                last_refresh_at=now.isoformat(),
                issuer=flow.token_url,
                actor_did=self.actor_did,
            )
        except Exception:  # reason: the mirror is display only; the row is committed
            _logger.warning("credential mirror update failed for %s", connection, exc_info=True)

    async def _report(
        self, connection: str, code: str, detail: str, row: CredentialRow | None
    ) -> None:
        await self.health.report(
            connection,
            HealthSignal(
                ok=False,
                source="credential",
                checked_by=self.actor_did,
                reason_code=classify(code, detail),
                detail=detail,
                credential_generation=row.generation if row is not None else None,
            ),
        )

    def _audit(self, action: str, connection: str, outcome: str, extra: dict[str, object]) -> None:
        if self.sink is None:
            return
        emit(
            AuditEvent(
                actor_did=self.actor_did,
                action=action,
                target=f"connection:{connection}",
                outcome=outcome,
                extra=dict(extra),
            ),
            self.sink,
        )


__all__ = [
    "DEFAULT_LIFETIME",
    "ESCALATION_DID",
    "EXPIRY_FLOOR",
    "RENEWAL_FRACTION",
    "RENEWER_DID",
    "TERMINAL_ERROR_CODES",
    "ClientCredentialSource",
    "CredentialRenewalError",
    "RefreshFn",
    "RefreshRequest",
    "RenewalPlanner",
    "RenewedCredential",
    "connection_client",
    "is_due",
]
