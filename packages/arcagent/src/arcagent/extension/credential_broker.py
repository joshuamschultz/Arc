"""P18-2 — the only producer of bearer values for running attachments.

An agent's attachment never holds a refresh token, a client secret, or a copy of
any sensitive field. It holds an :class:`AccessTokenHandle`: a capability bound to
one ``(connection, agent, agent DID, plan)`` that returns a fresh value on EVERY
call, and re-checks the grant on every call. Two consequences follow:

* **A re-auth reaches a running agent with no restart.** The next call reads the
  committed custody row, so a reconnected account's new token is simply there.
* **A revoke stops a live handle at once.** The grant is read from the deployment's
  connection registry each call; a handle kept after its grant was revoked (or
  after its connection was removed) is dead.

Every successful call is audited as ``secret.read`` with the coordinate, the kind
and the caller, and never the value. A refused call is audited as a deny.

Bearer values are returned only from the committed custody row, never from a
provider response (persist-before-use, :mod:`arcagent.extension.credentials`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from arctrust.audit import AuditEvent, AuditSink, emit

from arcagent.core.errors import ExtensionError
from arcagent.extension.connection_health import HealthReporter, HealthSignal
from arcagent.extension.credentials import EXPIRY_FLOOR, RenewalPlanner
from arcagent.extension.custody import CredentialRow, CredentialRowStore
from arcagent.extension.grants import ConnectionRegistry
from arcagent.extension.manifest import ExtensionManifest, OAuthFlow
from arcagent.extension.secrets import Secret

Clock = Callable[[], datetime]

#: Checked-by DID on health signals the broker itself raises.
BROKER_DID = "did:arc:system:credential-broker"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class CredentialPlan:
    """What a connection's manifest says its attachment may receive."""

    #: ``bearer()`` is the renewed access token when set.
    oauth: OAuthFlow | None
    #: A static bearer field (Slack ``user_token``); used when ``oauth`` is None.
    bearer_field: str | None
    #: Sensitive fields ``field()`` may return (``api_token``, ``database_dsn``).
    handle_fields: frozenset[str]
    #: Never returned: the refresh token and the OAuth client secret.
    withheld: frozenset[str]


def credential_plan(manifest: ExtensionManifest) -> CredentialPlan:
    """The plan a manifest implies. Pure."""
    oauth = manifest.oauth
    withheld = (
        frozenset({oauth.refresh_token_secret, oauth.client_secret_secret})
        if oauth is not None
        else frozenset()
    )
    sensitive = frozenset(declared.name for declared in manifest.secrets if declared.sensitive)
    bearer = manifest.credential.bearer if manifest.credential is not None else None
    return CredentialPlan(
        oauth=oauth,
        bearer_field=bearer,
        handle_fields=sensitive - withheld,
        withheld=withheld,
    )


class AccessTokenBroker:
    """Issues and serves credential handles for one agent, or for the operator.

    An agent's broker is BOUND to that agent's name and DID: it issues handles for
    no one else, so a forged ``agent`` argument cannot borrow another agent's
    grant. The operator's broker (``bound_agent=None``) serves the deployment's own
    management verbs (probe, install, health check), which the operator is the
    authority for.
    """

    def __init__(
        self,
        rows: CredentialRowStore,
        *,
        registry: Callable[[], ConnectionRegistry],
        renewals: RenewalPlanner | None,
        health: HealthReporter | None,
        sink: AuditSink | None = None,
        clock: Clock = _utcnow,
        bound_agent: str | None = None,
        bound_did: str | None = None,
    ) -> None:
        self._rows = rows
        self._registry = registry
        self._renewals = renewals
        self._health = health
        self._sink = sink
        self._clock = clock
        self._bound = (bound_agent, bound_did) if bound_agent is not None else None
        self._plans: dict[str, CredentialPlan] = {}

    @property
    def rows(self) -> CredentialRowStore:
        return self._rows

    def handle(
        self, connection: str, *, agent: str, agent_did: str, plan: CredentialPlan
    ) -> AccessTokenHandle:
        """A handle for a granted agent. Refused for any identity this broker is not."""
        if self._bound is None or self._bound != (agent, agent_did):
            self._audit(connection, "handle", agent_did, "deny", reason="identity_mismatch")
            raise _not_granted(connection, agent)
        self._plans[connection] = plan
        return AccessTokenHandle(self, connection, agent=agent, agent_did=agent_did, plan=plan)

    def operator_handle(
        self, connection: str, *, actor_did: str, plan: CredentialPlan
    ) -> AccessTokenHandle:
        """A handle for the deployment's own management verbs (no grant needed)."""
        if self._bound is not None:
            self._audit(connection, "handle", actor_did, "deny", reason="not_operator")
            raise _not_granted(connection, "operator")
        self._plans[connection] = plan
        return AccessTokenHandle(self, connection, agent=None, agent_did=actor_did, plan=plan)

    async def ensure_fresh(self, connection: str) -> bool:
        """Proactively renew one connection this broker has a plan for. False if none."""
        plan = self._plans.get(connection)
        if plan is None or plan.oauth is None or self._renewals is None:
            return False
        return await self._renewals.ensure_fresh(connection, flow=plan.oauth)

    async def bearer(
        self,
        connection: str,
        *,
        agent: str | None,
        agent_did: str,
        plan: CredentialPlan,
        force_refresh: bool = False,
    ) -> Secret:
        """The value to send as the bearer credential, fresh and from the committed row."""
        await self._require_grant(connection, agent, agent_did, slot="bearer")
        if plan.oauth is not None:
            token = await self._access_token(connection, plan.oauth, force=force_refresh)
            self._audit(connection, "access_token", agent_did, "allow")
            return token
        if plan.bearer_field is None:
            raise ExtensionError(
                code="CREDENTIAL_NO_BEARER",
                message=f"{connection!r} declares no bearer credential",
                details={"connection": connection},
            )
        found = await self._field(connection, plan.bearer_field, agent_did, required=True)
        if found is None:  # required=True raises instead; kept for the type checker
            raise ExtensionError(
                code="CREDENTIAL_MISSING",
                message=f"{connection!r} has no stored bearer credential",
                details={"connection": connection},
            )
        return found

    async def field(
        self,
        connection: str,
        name: str,
        *,
        agent: str | None,
        agent_did: str,
        plan: CredentialPlan,
        required: bool = True,
    ) -> Secret | None:
        """One declared sensitive field, or a refusal for anything withheld."""
        await self._require_grant(connection, agent, agent_did, slot=name)
        if name in plan.withheld or name not in plan.handle_fields:
            self._audit(connection, name, agent_did, "deny", reason="withheld")
            raise ExtensionError(
                code="CREDENTIAL_FIELD_WITHHELD",
                message=f"{name!r} of {connection!r} is not available to an attachment",
                details={"connection": connection, "field": name},
            )
        return await self._field(connection, name, agent_did, required=required)

    # --- internals ---------------------------------------------------------

    async def _require_grant(
        self, connection: str, agent: str | None, agent_did: str, *, slot: str
    ) -> None:
        if agent is None:
            return  # an operator handle; issued only by the operator's broker
        if self._bound is not None and self._bound != (agent, agent_did):
            self._audit(connection, slot, agent_did, "deny", reason="identity_mismatch")
            raise _not_granted(connection, agent)
        try:
            granted = await asyncio.to_thread(lambda: self._registry().get(connection).agents)
        except ExtensionError:
            granted = ()
        if agent not in granted:
            self._audit(connection, slot, agent_did, "deny", reason="not_granted")
            raise _not_granted(connection, agent)

    async def _row(self, connection: str) -> CredentialRow:
        row = await self._rows.read(connection)
        if row is None:
            raise ExtensionError(
                code="CREDENTIAL_MISSING",
                message=f"{connection!r} has no stored credential; connect it",
                details={"connection": connection},
            )
        return row

    async def _field(
        self, connection: str, name: str, agent_did: str, *, required: bool
    ) -> Secret | None:
        row = await self._row(connection)
        try:
            found = self._rows.open_field(row, name)
        except ExtensionError as exc:
            await self._report_unreadable(connection, exc, row)
            raise
        if found is None:
            if not required:
                return None
            raise ExtensionError(
                code="CREDENTIAL_MISSING",
                message=f"{connection!r} has no stored {name!r}",
                details={"connection": connection, "field": name},
            )
        self._audit(connection, name, agent_did, "allow", kind="field")
        return found

    async def _access_token(self, connection: str, flow: OAuthFlow, *, force: bool) -> Secret:
        row = await self._row(connection)
        if not force:
            fresh = self._open_fresh(connection, row)
            if fresh is not None:
                return fresh
        if self._renewals is None:
            await self._report(connection, "renewer_unavailable", "no renewer here", row)
            raise ExtensionError(
                code="CREDENTIAL_STALE",
                message=f"the access token for {connection!r} is stale and cannot be renewed here",
                details={"connection": connection, "retryable": True},
            )
        await self._renewals.ensure_fresh(connection, flow=flow, force=force)
        renewed = self._open_fresh(connection, await self._row(connection), floor=timedelta(0))
        if renewed is None:
            raise ExtensionError(
                code="CREDENTIAL_STALE",
                message=f"the access token for {connection!r} could not be renewed",
                details={"connection": connection, "retryable": True},
            )
        return renewed

    def _open_fresh(
        self, connection: str, row: CredentialRow, *, floor: timedelta = EXPIRY_FLOOR
    ) -> Secret | None:
        token = self._rows.open_access(row)
        if token is None or token.expires_at - self._clock() <= floor:
            return None
        return token.token

    async def _report_unreadable(
        self, connection: str, exc: ExtensionError, row: CredentialRow
    ) -> None:
        if exc.code == "CREDENTIAL_UNREADABLE":
            await self._report(connection, "credential_unreadable", exc.message, row)

    async def _report(self, connection: str, code: str, detail: str, row: CredentialRow) -> None:
        if self._health is None:
            return
        await self._health.report(
            connection,
            HealthSignal(
                ok=False,
                source="credential",
                checked_by=BROKER_DID,
                reason_code=code,  # type: ignore[arg-type] # reason: callers pass table codes
                detail=detail,
                credential_generation=row.generation,
            ),
        )

    def _audit(
        self,
        connection: str,
        slot: str,
        actor_did: str,
        outcome: str,
        *,
        kind: str = "access_token",
        reason: str | None = None,
    ) -> None:
        if self._sink is None:
            return
        extra: dict[str, object] = {"store": "sealed", "kind": kind}
        if reason is not None:
            extra["reason"] = reason
        emit(
            AuditEvent(
                actor_did=actor_did,
                action="secret.read",
                target=f"secret:{connection}/{slot}",
                outcome=outcome,
                extra=extra,
            ),
            self._sink,
        )


def _not_granted(connection: str, agent: str) -> ExtensionError:
    return ExtensionError(
        code="CREDENTIAL_NOT_GRANTED",
        message=f"{agent!r} is not granted {connection!r}",
        details={"connection": connection, "agent": agent},
    )


class AccessTokenHandle:
    """A capability bound to (connection, agent, agent DID, plan). Holds no secret."""

    __slots__ = ("_agent", "_agent_did", "_broker", "_connection", "_force", "_plan")

    def __init__(
        self,
        broker: AccessTokenBroker,
        connection: str,
        *,
        agent: str | None,
        agent_did: str,
        plan: CredentialPlan,
    ) -> None:
        self._broker = broker
        self._connection = connection
        self._agent = agent
        self._agent_did = agent_did
        self._plan = plan
        self._force = False

    @property
    def connection(self) -> str:
        return self._connection

    async def bearer(self) -> Secret:
        """The bearer value to send now. Fresh, committed, grant re-checked."""
        force, self._force = self._force, False
        return await self._broker.bearer(
            self._connection,
            agent=self._agent,
            agent_did=self._agent_did,
            plan=self._plan,
            force_refresh=force,
        )

    async def field(self, name: str) -> Secret:
        """A declared sensitive field. Raises when it is withheld or absent."""
        found = await self._broker.field(
            self._connection, name, agent=self._agent, agent_did=self._agent_did, plan=self._plan
        )
        if found is None:  # required=True raises instead; kept for the type checker
            raise ExtensionError(
                code="CREDENTIAL_MISSING",
                message=f"{self._connection!r} has no stored {name!r}",
                details={"connection": self._connection, "field": name},
            )
        return found

    async def maybe_field(self, name: str) -> Secret | None:
        """A declared optional sensitive field, or None when it was never supplied."""
        return await self._broker.field(
            self._connection,
            name,
            agent=self._agent,
            agent_did=self._agent_did,
            plan=self._plan,
            required=False,
        )

    async def invalidate(self) -> None:
        """The provider answered 401: the next ``bearer()`` forces a renewal."""
        self._force = True

    def __repr__(self) -> str:
        return f"AccessTokenHandle({self._connection})"

    __str__ = __repr__


class CredentialRenewals:
    """Where an agent's live credential broker is published for its sync loop.

    One per agent, shared like the source catalog: the connectors module binds
    the broker it attached with, and the connected-data service asks it to renew a
    connection proactively before a sync reads the provider.
    """

    def __init__(self) -> None:
        self._broker: AccessTokenBroker | None = None

    def bind(self, broker: AccessTokenBroker | None) -> None:
        self._broker = broker

    async def ensure_fresh(self, connection: str) -> bool:
        """Renew ``connection`` if it is an OAuth connection that is due. False otherwise."""
        broker = self._broker
        return False if broker is None else await broker.ensure_fresh(connection)


__all__ = [
    "BROKER_DID",
    "AccessTokenBroker",
    "AccessTokenHandle",
    "CredentialPlan",
    "CredentialRenewals",
    "credential_plan",
]
