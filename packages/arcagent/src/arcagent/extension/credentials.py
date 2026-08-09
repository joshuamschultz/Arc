"""SPEC-062 COMP-011 — keeping a connected account alive without a human.

Renewal is driven by the clock, not by a failure: a credential is refreshed once
three quarters of its lifetime has elapsed (REQ-287). Waiting for a call to
return 401 means every connection breaks at least once per credential lifetime,
in the middle of whatever the agent was doing.

The hard part is REQ-288, and it is not obvious. Any provider that rotates its
refresh token issues that token *single-use*: the moment
one is exchanged it is dead. So two renewals racing for the same account do not
merely duplicate work, they destroy the account: the second presents a token the
authorization server has already consumed, gets ``invalid_grant``, and a late
writer can persist that rejected token over the working one. Nothing recovers
that except a human re-consenting.

Three things together prevent it, and all three are needed:

1. **One writer per account.** A per-account lock, not a global one, so a slow
   renewal on one connection cannot stall every other connection's renewal.
2. **A re-read after acquiring the lock.** The lock alone only *serialises* the
   two renewals; the waiter would still exchange the now-consumed token. After
   acquiring, the waiter re-reads the durable expiry, sees the credential is no
   longer due, and returns without touching the authorization server.
3. **Value before metadata.** The new token is persisted first. A crash between
   the two persists leaves stale metadata, which merely renews early next time.
   The reverse order discards the only copy of a token whose predecessor is
   already consumed — an unrecoverable connection.

Failure classification is the other half (REQ-289). ``invalid_grant`` and the
consent codes mean a human must act, so retrying is not merely useless — it
burns the connection further. Those stop immediately, mark the connection, and
escalate through the operator approval path, which is signed and pinned to an
operator identity. They never go through agent chat, where an approval is just
text a model can be talked into producing (ASI09).

This component holds no credential state of its own: values live in
:mod:`arcagent.extension.secrets` and metadata in the connection state store.
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
from arcagent.extension.secrets import Secret, SecretRef, SecretStore
from arcagent.extension.state import ConnectionHealth

_logger = logging.getLogger("arcagent.extension.credentials")

#: OAuth error codes that mean a human must re-consent. Retrying one of these is
#: not just futile — it consumes attempts against a connection that is already
#: broken and delays telling the operator.
TERMINAL_ERROR_CODES = frozenset({"invalid_grant", "consent_required", "interaction_required"})

#: Actor recorded when the lifecycle itself marks a connection as needing a human.
#: Nobody requested that mark — a terminal renewal failure caused it — so the
#: record names the component, the pattern
#: :data:`~arcagent.extension.contract_ledger.LEDGER_DID` already sets.
ESCALATION_DID = "did:arc:arcagent:credential-lifecycle"

#: Fraction of a credential's lifetime that may elapse before renewal is due.
RENEWAL_FRACTION = 0.75

#: Nominal lifetime assumed when a connector declares none — one hour is the
#: prevailing OAuth access-token lifetime.
DEFAULT_LIFETIME = timedelta(hours=1)

_MAX_ATTEMPTS = 4
_BASE_BACKOFF_SECONDS = 1.0
_MAX_BACKOFF_SECONDS = 30.0


class CredentialRenewalError(ExtensionError):
    """A renewal attempt failed. ``terminal`` decides whether retrying is sane."""

    def __init__(self, *, error_code: str, message: str) -> None:
        super().__init__(
            code="CREDENTIAL_RENEWAL_FAILED",
            message=message,
            details={"error_code": error_code},
        )
        self.error_code = error_code

    @property
    def terminal(self) -> bool:
        """True when only a human re-consent can fix this."""
        return self.error_code in TERMINAL_ERROR_CODES


@dataclass(frozen=True)
class ConnectedAccount:
    """One connected account whose credential renews itself.

    Named by the connection, not by an agent: the account is the deployment's and
    the renewal is one exchange for every agent granted it. Keying this by agent
    would mean two grantees racing to spend the same single-use refresh token,
    which is the unrecoverable failure this module is built around (REQ-288).
    """

    connection: str
    field: str = "refresh_token"
    lifetime: timedelta = DEFAULT_LIFETIME

    @property
    def secret_ref(self) -> SecretRef:
        """Where this account's renewable credential is stored."""
        return SecretRef(connection=self.connection, field=self.field)

    @property
    def key(self) -> str:
        return self.connection


@dataclass(frozen=True)
class RenewedCredential:
    """What an exchange with the authorization server produced."""

    value: str
    expires_at: datetime
    issued_at: datetime | None = None
    issuer: str | None = None
    audience: str | None = None


class ConnectionCredentialState(Protocol):
    """The credential coordinates a connection record carries — never a value."""

    @property
    def credential_expires_at(self) -> str | None: ...

    @property
    def credential_issuer(self) -> str | None: ...

    @property
    def credential_audience(self) -> str | None: ...


class CredentialMetadataStore(Protocol):
    """The slice of the connection state store this component needs.

    A structural Protocol rather than a concrete import, matching the precedent
    in :mod:`arcagent.extension.state`: the lifecycle stays testable against any
    conforming plane, and the seam is narrow enough to read.
    """

    async def get(self, connection: str) -> ConnectionCredentialState | None: ...

    async def record_credential_metadata(
        self,
        connection: str,
        *,
        expires_at: str | None = None,
        issuer: str | None = None,
        audience: str | None = None,
        last_refresh_at: str | None = None,
        actor_did: str,
    ) -> bool: ...

    async def set_health(
        self, connection: str, health: ConnectionHealth, *, actor_did: str
    ) -> bool: ...


class OperatorEscalation(Protocol):
    """The operator approval path. Deliberately not a chat surface (ASI09)."""

    async def request_operator_attention(
        self, *, connection: str, reason: str, detail: str
    ) -> None: ...


RenewFn = Callable[[Secret], Awaitable[RenewedCredential]]
Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class CredentialLifecycle:
    """Proactive, single-writer credential renewal for connected accounts.

    ``escalation`` has no default on purpose: a lifecycle wired without an
    operator path would discover terminal failures and have nowhere to report
    them, which is exactly the silent-failure shape this component exists to
    remove.
    """

    secrets: SecretStore
    state: CredentialMetadataStore
    escalation: OperatorEscalation
    sink: AuditSink | None = None
    clock: Clock = _utcnow
    sleep: Sleep = asyncio.sleep
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False, repr=False)

    async def ensure_fresh(
        self, account: ConnectedAccount, *, renew: RenewFn, caller_did: str
    ) -> bool:
        """Renew ``account``'s credential if its lifetime is far enough gone.

        Returns True when a renewal happened. Raises
        :class:`CredentialRenewalError` when renewal was needed and could not be
        completed — the connection is marked and, for a terminal failure, the
        operator has already been asked to act.
        """
        if not self._is_due(await self._expiry(account), account):
            return False

        async with self._lock_for(account):
            # Re-read after acquiring: a renewal that completed while this call
            # waited has already rotated the token, and exchanging the consumed
            # one is what permanently breaks the connection.
            if not self._is_due(await self._expiry(account), account):
                return False
            renewed = await self._exchange(account, renew, caller_did)
            await self._persist(account, renewed, caller_did)
            return True

    async def _exchange(
        self, account: ConnectedAccount, renew: RenewFn, caller_did: str
    ) -> RenewedCredential:
        """Run the exchange, retrying only failures a retry can actually fix."""
        current = await self.secrets.get(account.secret_ref, caller_did=caller_did)
        if current is None:
            await self._needs_attention(
                account, "credential_missing", "the connection has never been authorized"
            )
            raise ExtensionError(
                code="CREDENTIAL_MISSING",
                message=f"no stored credential for {account.key}; authorize the connection",
                details={"connection": account.key},
            )

        for attempt in range(_MAX_ATTEMPTS):
            try:
                return await renew(current)
            except CredentialRenewalError as exc:
                if exc.terminal:
                    await self._needs_attention(account, exc.error_code, exc.message)
                    self._audit("credential.renew", account, caller_did, "deny", exc.error_code)
                    raise
                if attempt == _MAX_ATTEMPTS - 1:
                    await self._mark(account, "degraded", caller_did)
                    self._audit("credential.renew", account, caller_did, "error", exc.error_code)
                    raise
                await self.sleep(min(_BASE_BACKOFF_SECONDS * 2**attempt, _MAX_BACKOFF_SECONDS))
        raise AssertionError("unreachable: the loop either returns or raises")

    async def _persist(
        self, account: ConnectedAccount, renewed: RenewedCredential, caller_did: str
    ) -> None:
        """Store the value, then the metadata — never the other way round."""
        await self.secrets.put(account.secret_ref, renewed.value, caller_did=caller_did)
        stored = await self.state.record_credential_metadata(
            account.connection,
            expires_at=renewed.expires_at.isoformat(),
            issuer=renewed.issuer,
            audience=renewed.audience,
            last_refresh_at=self.clock().isoformat(),
            actor_did=caller_did,
        )
        if not stored:
            # The store refuses to patch a connection it does not hold, so a
            # silent False here means the new expiry was never durable. The next
            # pass would read the OLD expiry, find it due, and exchange a refresh
            # token this call has already consumed — the unrecoverable outcome
            # this whole component is built around (REQ-288).
            raise ExtensionError(
                code="CONNECTION_STATE_MISSING",
                message=(
                    f"{account.key} has no connection record, so the renewed credential's "
                    f"expiry could not be stored"
                ),
                details={"connection": account.key},
            )
        self._audit("credential.renew", account, caller_did, "allow", None)

    async def _needs_attention(self, account: ConnectedAccount, reason: str, detail: str) -> None:
        """Mark the connection and ask the operator — never the agent's chat."""
        await self._mark(account, "needs_attention", ESCALATION_DID)
        await self.escalation.request_operator_attention(
            connection=account.connection, reason=reason, detail=detail
        )

    async def _mark(
        self, account: ConnectedAccount, health: ConnectionHealth, actor_did: str
    ) -> None:
        """Set a connection's health, and say so when the mark landed nowhere.

        Never raises: both callers are already reporting the failure that caused
        the mark, and replacing that report with a bookkeeping error would lose
        the reason the operator actually needs. Silence is what is unacceptable —
        a dashboard showing ``healthy`` for a connection nothing can renew.
        """
        if not await self.state.set_health(account.connection, health, actor_did=actor_did):
            _logger.error(
                "connection %s has no state record; its health could not be marked %s",
                account.key,
                health,
            )

    async def _expiry(self, account: ConnectedAccount) -> datetime | None:
        record = await self.state.get(account.connection)
        raw = record.credential_expires_at if record is not None else None
        if raw is None:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ExtensionError(
                code="CREDENTIAL_EXPIRY_UNREADABLE",
                message=f"credential expiry for {account.key} is not a timestamp",
                details={"connection": account.key},
            ) from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    def _is_due(self, expires_at: datetime | None, account: ConnectedAccount) -> bool:
        """True once ``RENEWAL_FRACTION`` of the nominal lifetime has elapsed.

        A credential with no known expiry — a static API key — has no lifetime to
        be a fraction of and is left alone.
        """
        if expires_at is None:
            return False
        return expires_at - self.clock() <= account.lifetime * (1 - RENEWAL_FRACTION)

    def _lock_for(self, account: ConnectedAccount) -> asyncio.Lock:
        return self._locks.setdefault(account.key, asyncio.Lock())

    def _audit(
        self,
        action: str,
        account: ConnectedAccount,
        caller_did: str,
        outcome: str,
        error_code: str | None,
    ) -> None:
        if self.sink is None:
            return
        emit(
            AuditEvent(
                actor_did=caller_did,
                action=action,
                target=f"connection:{account.key}",
                outcome=outcome,
                extra={"error_code": error_code} if error_code else {},
            ),
            self.sink,
        )


__all__ = [
    "DEFAULT_LIFETIME",
    "ESCALATION_DID",
    "RENEWAL_FRACTION",
    "TERMINAL_ERROR_CODES",
    "ConnectedAccount",
    "ConnectionCredentialState",
    "CredentialLifecycle",
    "CredentialMetadataStore",
    "CredentialRenewalError",
    "OperatorEscalation",
    "RenewFn",
    "RenewedCredential",
]
