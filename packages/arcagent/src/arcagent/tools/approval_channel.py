"""Mechanical approval channel — arcstore-backed operator handoff (SPEC-035).

Wires :class:`~arcagent.tools.human_gate.HumanGate` to the shared arcstore
``approvals`` directory instead of agent chat. When a trifecta-completing call is
blocked, this channel writes a ``pending`` row and polls until an operator
resolves it out-of-band via the ``arc approve`` CLI or the arcui operator surface
— both of which attach an operator-signed :class:`~arctrust.policy.ApprovalGrant`.
The gate then verifies that grant against the operator public key.

Why this is spoof-proof: approval never rides on a chat message (which a
prompt-injected or foreign message could forge). It is an operator-authenticated
write to the store, and the grant's signature is verified by the gate. The agent
holds no path to mint it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from arcstore.approvals import ApprovalStore, PendingApproval
from arcstore.backends import ArcStoreBackend
from arcstore.standing_grants import StandingGrantStore
from arctrust.policy import ApprovalGrant, ScenarioGrant, grant_from_wire, scenario_grant_from_wire

from arcagent.tools.human_gate import ApprovalRequest

_logger = logging.getLogger("arcagent.approval_channel")


class ArcStoreApprovalChannel:
    """An :data:`~arcagent.tools.human_gate.ApprovalChannel` over ``ApprovalStore``.

    Also the gate's :class:`~arcagent.tools.human_gate.StandingGrantSource`: the
    operator's "Always allow" rows live in the same store (``standing_grants``),
    so the agent reads them through the same lazily opened backend.

    Parameters
    ----------
    store:
        A ready ``ApprovalStore`` (tests / already-open case). Mutually exclusive
        with ``store_opener``.
    store_opener:
        Async factory that opens the shared ``ApprovalStore`` (same DB arcui /
        arccli read). Awaited at most once, on the first block — so an agent that
        never trips the trifecta never opens the backend.
    id_factory:
        Returns a unique request id per call (injected so tests are deterministic
        and the module needs no ambient randomness).
    agent_label:
        Friendly agent handle surfaced to the operator (e.g. ``"josh_agent"``).
    poll_interval_seconds:
        How often to re-read the row while awaiting a decision.
    ttl_seconds:
        Advisory expiry stamped on the row so the operator surfaces can grey out
        a request the agent has already stopped waiting on. The gate's own
        timeout is authoritative for failing closed.
    """

    def __init__(
        self,
        store: ApprovalStore | None = None,
        *,
        standing_store: StandingGrantStore | None = None,
        store_opener: Callable[[], Awaitable[tuple[ApprovalStore, ArcStoreBackend]]] | None = None,
        id_factory: Callable[[], str],
        agent_label: str = "",
        poll_interval_seconds: float = 1.0,
        ttl_seconds: float = 300.0,
    ) -> None:
        if (store is None) == (store_opener is None):
            raise ValueError("provide exactly one of store or store_opener")
        self._store = store
        self._standing_store = standing_store
        self._backend: ArcStoreBackend | None = None
        self._store_opener = store_opener
        self._open_lock = asyncio.Lock()
        self._id_factory = id_factory
        self._agent_label = agent_label
        self._poll = poll_interval_seconds
        self._ttl = ttl_seconds

    async def _ensure_store(self) -> ApprovalStore:
        """Return the store, opening it once on first use (opener case)."""
        if self._store is not None:
            return self._store
        opener = self._store_opener
        if opener is None:  # unreachable: constructor requires one of the two
            raise RuntimeError("approval channel has neither store nor opener")
        async with self._open_lock:
            if self._store is None:
                self._store, self._backend = await opener()
                self._standing_store = StandingGrantStore(self._backend)
        return self._store

    async def _ensure_standing_store(self) -> StandingGrantStore | None:
        """The standing-grant store on the same backend, or None if not wired."""
        await self._ensure_store()
        return self._standing_store

    async def active_standing_grants(self, agent_did: str) -> list[tuple[str, ScenarioGrant]]:
        """Every active "Always allow" row of this agent, decoded (unverified).

        A row that does not decode is skipped — the gate verifies what remains,
        so an unreadable or forged row can only ever cost a prompt.
        """
        standing = await self._ensure_standing_store()
        if standing is None:
            return []
        grants: list[tuple[str, ScenarioGrant]] = []
        for row in await standing.active_for(agent_did):
            try:
                grants.append((row.id, scenario_grant_from_wire(row.grant)))
            except Exception:  # reason: a malformed row is ignored, never trusted
                _logger.warning("standing grant %s is malformed; ignored", row.id)
        return grants

    async def record_standing_grant_use(self, grant_id: str, agent_did: str) -> bool:
        """Count one use, only while the row is still active."""
        standing = await self._ensure_standing_store()
        if standing is None:
            return False
        return await standing.record_use(grant_id, actor_did=agent_did)

    async def close(self) -> None:
        """Release the lazily opened backend after active approvals finish."""
        backend, self._backend = self._backend, None
        self._store = None
        self._standing_store = None
        if backend is not None:
            await backend.stop()

    async def __call__(self, request: ApprovalRequest) -> ApprovalGrant | None:
        store = await self._ensure_store()
        expires_at = (datetime.now(UTC) + timedelta(seconds=self._ttl)).isoformat()
        pending = PendingApproval(
            id=self._id_factory(),
            agent_did=request.agent_did,
            agent_label=self._agent_label,
            tool=request.tool_name,
            legs=sorted(request.legs),
            call_hash=request.call_hash,
            session_id=request.session_id,
            arguments=request.arguments,
            provenance=request.leg_provenance,
            destination=request.destination,
            grant_tool=request.grant_tool,
            standing_eligible=request.standing_eligible,
            expires_at=expires_at,
        )
        await store.create(pending)

        # Poll until an operator resolves the row. The gate wraps this in its own
        # timeout, so a never-resolved request is cancelled here and fails closed.
        try:
            while True:
                row = await store.get(pending.id)
                if row is None or row.status in ("denied", "expired"):
                    return None
                if row.status == "approved" and row.grant is not None:
                    return grant_from_wire(row.grant)
                await asyncio.sleep(self._poll)
        finally:
            await self._expire_if_still_pending(store, pending)

    async def _expire_if_still_pending(
        self, store: ApprovalStore, pending: PendingApproval
    ) -> None:
        """Close the row when nobody is waiting on it any more.

        Runs on every exit: gate timeout, run cancelled, run ended. ``resolve`` is
        conditional on ``pending``, so an operator decision that already landed is
        never overwritten. Shielded so a second cancel cannot leave the row open.
        """
        try:
            await asyncio.shield(
                store.resolve(
                    pending.id,
                    status="expired",
                    actor_did=pending.agent_did,
                    resolved_by=pending.agent_did,
                    note="the agent stopped waiting",
                )
            )
        except Exception:  # reason: cleanup must not mask the gate's own outcome
            _logger.warning("could not expire approval %s", pending.id, exc_info=True)


__all__ = ["ArcStoreApprovalChannel"]
