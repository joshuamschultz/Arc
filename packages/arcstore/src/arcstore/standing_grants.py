"""``standing_grants`` domain — the durable home of an operator's "Always allow".

When an operator answers a trifecta approval with "Always allow" (SPEC-035 OQ-3,
ruled 2026-10-03), the operator surface signs an interactive
:class:`arctrust.policy.ScenarioGrant` and stores it here. The agent's
``HumanGate`` reads the ACTIVE rows for its own DID fresh on every gate hit and
verifies each signature itself, so:

- a revoke (status ``revoked``) takes effect on the very next call;
- a forged or edited row is inert — the store holds data, the gate holds trust.

Same collection-keyed mutable plane as :mod:`arcstore.approvals` (no new table).
One row per scope: the id is derived from agent + tool + composition +
destination, so granting the same scope twice refreshes one row.
"""

from __future__ import annotations

import builtins
import hashlib
import json
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal, Protocol

from arctrust.audit import AuditSink
from arctrust.policy import (
    INTERACTIVE_ORIGIN,
    ApprovalAuthority,
    grant_to_wire,
    scenario_grant_to_wire,
    sign_approval_for_hash,
    sign_scenario_grant,
)
from pydantic import BaseModel, ConfigDict, Field

from arcstore.approvals import ApprovalStore, PendingApproval

StandingGrantStatus = Literal["active", "revoked"]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def standing_grant_id(
    *, agent_did: str, tool: str, composition: list[str], destination: str
) -> str:
    """Stable id of one grant scope — order-independent over the composition."""
    scope = json.dumps(
        {
            "agent_did": agent_did,
            "tool": tool,
            "composition": sorted(composition),
            "destination": destination,
        },
        sort_keys=True,
    )
    return "sg-" + hashlib.sha256(scope.encode("utf-8")).hexdigest()[:24]


class StandingGrant(BaseModel):
    """One operator "Always allow" — what it covers, who granted it, how often used.

    ``grant`` holds the operator-signed ``ScenarioGrant`` in wire form
    (``arctrust.policy.scenario_grant_to_wire``); everything else is display
    and bookkeeping the gate never trusts.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    agent_did: str
    agent_label: str = ""
    tool: str
    composition: list[str] = Field(default_factory=list)
    #: Destination class of the approved egress (a connection id, a recipient,
    #: an origin). Empty when the approved call added no egress.
    destination: str = ""
    grant: dict[str, Any]
    status: StandingGrantStatus = "active"
    granted_by: str
    granted_at: str | None = None
    source_approval_id: str = ""
    revoked_by: str | None = None
    revoked_at: str | None = None
    use_count: int = 0
    last_used_at: str | None = None


class MutableStandingGrantBackend(Protocol):
    """The mutable-plane primitives :class:`StandingGrantStore` needs."""

    async def mutable_write(
        self,
        collection: str,
        key: str,
        value: dict[str, Any],
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> None: ...

    async def mutable_read(self, collection: str, key: str) -> dict[str, Any] | None: ...

    async def mutable_query(
        self, collection: str, *, where: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]: ...

    async def update_if(
        self,
        collection: str,
        key: str,
        patch: dict[str, Any],
        where: dict[str, Any],
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> bool: ...

    async def update_if_increment(
        self,
        collection: str,
        key: str,
        patch: dict[str, Any],
        deltas: dict[str, int | float],
        where: dict[str, Any],
        *,
        actor_did: str,
        sink: Any | None = None,
    ) -> bool: ...


class StandingGrantStore:
    """Standing grants over the mutable plane's ``"standing_grants"`` collection."""

    _COLLECTION: ClassVar[str] = "standing_grants"

    def __init__(
        self, backend: MutableStandingGrantBackend, *, sink: AuditSink | None = None
    ) -> None:
        self._backend = backend
        self._sink = sink

    async def put(self, grant: StandingGrant, *, actor_did: str) -> StandingGrant:
        """Store (or refresh) one scope as ``active`` with a zeroed use count."""
        row = grant.model_copy(
            update={
                "status": "active",
                "granted_at": _now(),
                "revoked_by": None,
                "revoked_at": None,
                "use_count": 0,
                "last_used_at": None,
            }
        )
        await self._backend.mutable_write(
            self._COLLECTION,
            row.id,
            row.model_dump(mode="json"),
            actor_did=actor_did,
            sink=self._sink,
        )
        return row

    async def get(self, grant_id: str) -> StandingGrant | None:
        raw = await self._backend.mutable_read(self._COLLECTION, grant_id)
        return StandingGrant.model_validate(raw) if raw is not None else None

    async def list(
        self, *, agent_did: str | None = None, status: StandingGrantStatus | None = None
    ) -> list[StandingGrant]:
        where: dict[str, Any] = {}
        if agent_did is not None:
            where["agent_did"] = agent_did
        if status is not None:
            where["status"] = status
        rows = await self._backend.mutable_query(self._COLLECTION, where=where)
        items = [StandingGrant.model_validate(row) for row in rows]
        items.sort(key=lambda g: g.granted_at or "", reverse=True)
        return items

    async def active_for(self, agent_did: str) -> builtins.list[StandingGrant]:
        """The grants an agent's gate may consider — read fresh, never cached."""
        return await self.list(agent_did=agent_did, status="active")

    async def revoke(self, grant_id: str, *, actor_did: str) -> StandingGrant | None:
        """Revoke an active grant (race-safe). Returns None if it was not active."""
        won = await self._backend.update_if(
            self._COLLECTION,
            grant_id,
            {"status": "revoked", "revoked_by": actor_did, "revoked_at": _now()},
            where={"status": "active"},
            actor_did=actor_did,
            sink=self._sink,
        )
        return await self.get(grant_id) if won else None

    async def record_use(self, grant_id: str, *, actor_did: str) -> bool:
        """Count one use of an active grant; False when it is gone or revoked."""
        return await self._backend.update_if_increment(
            self._COLLECTION,
            grant_id,
            {"last_used_at": _now()},
            {"use_count": 1},
            where={"status": "active"},
            actor_did=actor_did,
            sink=self._sink,
        )


class StandingGrantRefusedError(Exception):
    """The request cannot be made to stand (federal tier, or not pending)."""


async def approve_always(
    approvals: ApprovalStore,
    standing: StandingGrantStore,
    row: PendingApproval,
    operator: ApprovalAuthority,
) -> tuple[PendingApproval, StandingGrant]:
    """Resolve ``row`` approved AND store an operator "Always allow" for its scope.

    The one operation behind arcui's "Always allow" button and ``arc approve
    <id> --always``. The waiting call gets its one-shot grant exactly as with a
    plain Approve; the standing grant is an interactive ``ScenarioGrant`` signed
    by the same operator over agent + verb + composition + destination.

    Raises:
        StandingGrantRefusedError: the agent marked the request ineligible (federal
            never stands) or the request is no longer pending.
    """
    if not row.standing_eligible:
        raise StandingGrantRefusedError("this request cannot be made to stand at its tier")
    if row.status != "pending":
        raise StandingGrantRefusedError(f"request {row.id!r} is already {row.status}")
    tool = row.grant_tool or row.tool
    destination = row.destination or ""
    scenario = sign_scenario_grant(
        operator=operator,
        agent_did=row.agent_did,
        tool_name=tool,
        composition=frozenset(row.legs),
        origin=INTERACTIVE_ORIGIN,
        connection=destination,
    )
    grant_id = standing_grant_id(
        agent_did=row.agent_did, tool=tool, composition=list(row.legs), destination=destination
    )
    resolved = await approvals.resolve(
        row.id,
        status="approved",
        actor_did=operator.did,
        resolved_by=operator.did,
        grant=grant_to_wire(sign_approval_for_hash(row.call_hash, operator)),
        note=f"always allow: {grant_id}",
    )
    if resolved is None:
        raise StandingGrantRefusedError(f"request {row.id!r} raced out of pending")
    stored = await standing.put(
        StandingGrant(
            id=grant_id,
            agent_did=row.agent_did,
            agent_label=row.agent_label,
            tool=tool,
            composition=sorted(row.legs),
            destination=destination,
            grant=scenario_grant_to_wire(scenario),
            granted_by=operator.did,
            source_approval_id=row.id,
        ),
        actor_did=operator.did,
    )
    return resolved, stored


__all__ = [
    "MutableStandingGrantBackend",
    "StandingGrant",
    "StandingGrantRefusedError",
    "StandingGrantStatus",
    "StandingGrantStore",
    "approve_always",
    "standing_grant_id",
]
