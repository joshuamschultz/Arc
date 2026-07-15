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
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from arcstore.approvals import ApprovalStore, PendingApproval
from arctrust.policy import ApprovalGrant, grant_from_wire

from arcagent.tools.human_gate import ApprovalRequest


class ArcStoreApprovalChannel:
    """An :data:`~arcagent.tools.human_gate.ApprovalChannel` over ``ApprovalStore``.

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
        store_opener: Callable[[], Awaitable[ApprovalStore]] | None = None,
        id_factory: Callable[[], str],
        agent_label: str = "",
        poll_interval_seconds: float = 1.0,
        ttl_seconds: float = 300.0,
    ) -> None:
        if (store is None) == (store_opener is None):
            raise ValueError("provide exactly one of store or store_opener")
        self._store = store
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
                self._store = await opener()
        return self._store

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
            expires_at=expires_at,
        )
        await store.create(pending)

        # Poll until an operator resolves the row. The gate wraps this in its own
        # timeout, so a never-resolved request is cancelled here and fails closed.
        while True:
            row = await store.get(pending.id)
            if row is None or row.status in ("denied", "expired"):
                return None
            if row.status == "approved" and row.grant is not None:
                return grant_from_wire(row.grant)
            await asyncio.sleep(self._poll)


__all__ = ["ArcStoreApprovalChannel"]
