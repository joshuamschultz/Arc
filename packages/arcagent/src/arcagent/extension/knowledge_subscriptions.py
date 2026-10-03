"""Which agents read which connection's shared knowledge store (P18-4).

A connection granted to several agents is synced once into one store that no
agent owns. A :class:`KnowledgeSubscription` says one agent reads one
connection's store: it is written by the connected-data module only after that
agent's own approved mapping is verified, and deleted the moment its grant is
revoked. Every read is checked against it, and every surface that shows a
connection's sync rows per agent (the card) expands the store's one row to its
subscribers through it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

#: The identity a connection's shared store is filed under: in the sync store, in
#: the store's own audit events, in its document pools. Never an agent's DID.
PRINCIPAL_PREFIX = "did:arc:knowledge:"


def store_key(connection_id: str) -> str:
    """A filesystem- and key-safe name for one connection's shared store."""
    return hashlib.sha256(connection_id.encode("utf-8")).hexdigest()[:32]


def knowledge_principal(connection_id: str) -> str:
    """The non-agent principal that owns one connection's sync row and store."""
    return f"{PRINCIPAL_PREFIX}{store_key(connection_id)}"


def is_knowledge_principal(did: str) -> bool:
    return did.startswith(PRINCIPAL_PREFIX)


@dataclass(frozen=True)
class KnowledgeSubscription:
    """Agent ``agent_did`` reads connection ``connection_id``'s shared store."""

    agent_did: str
    connection_id: str
    #: The shared document pool's id (its incarnation included).
    source_id: str
    #: The agent's own approval the subscription was verified against.
    approval_id: str
    #: The embedding profile the agent queries with.
    profile: str

    @property
    def principal(self) -> str:
        return knowledge_principal(self.connection_id)


class KnowledgeSubscriptions:
    """Durable subscriptions over ArcStore's mutable plane, keyed per agent and connection."""

    COLLECTION = "connected_knowledge_subscriptions"

    def __init__(self, backend: Any, *, actor_did: str) -> None:
        self._backend = backend
        self._actor_did = actor_did

    async def put(self, subscription: KnowledgeSubscription) -> None:
        await self._backend.mutable_write(
            self.COLLECTION,
            self._key(subscription.agent_did, subscription.connection_id),
            {
                "agent_did": subscription.agent_did,
                "connection_id": subscription.connection_id,
                "source_id": subscription.source_id,
                "approval_id": subscription.approval_id,
                "profile": subscription.profile,
            },
            actor_did=self._actor_did,
        )

    async def get(self, agent_did: str, connection_id: str) -> KnowledgeSubscription | None:
        row = await self._backend.mutable_read(
            self.COLLECTION, self._key(agent_did, connection_id)
        )
        return _subscription(row)

    async def delete(self, agent_did: str, connection_id: str) -> None:
        await self._backend.mutable_delete(
            self.COLLECTION, self._key(agent_did, connection_id), actor_did=self._actor_did
        )

    async def for_agent(self, agent_did: str) -> list[KnowledgeSubscription]:
        rows = await self._backend.mutable_query(self.COLLECTION, where={"agent_did": agent_did})
        return _subscriptions(rows)

    async def for_connection(self, connection_id: str) -> list[KnowledgeSubscription]:
        rows = await self._backend.mutable_query(
            self.COLLECTION, where={"connection_id": connection_id}
        )
        return _subscriptions(rows)

    @staticmethod
    def _key(agent_did: str, connection_id: str) -> str:
        return hashlib.sha256(f"{agent_did}\0{connection_id}".encode()).hexdigest()


def _subscription(row: Any) -> KnowledgeSubscription | None:
    if not isinstance(row, dict):
        return None
    fields = ("agent_did", "connection_id", "source_id", "approval_id", "profile")
    values = [row.get(name) for name in fields]
    if not all(isinstance(value, str) and value for value in values):
        return None
    return KnowledgeSubscription(*[str(value) for value in values])


def _subscriptions(rows: list[dict[str, Any]]) -> list[KnowledgeSubscription]:
    parsed = (_subscription(row) for row in rows)
    return sorted(
        (row for row in parsed if row is not None),
        key=lambda row: (row.connection_id, row.agent_did),
    )


__all__ = [
    "PRINCIPAL_PREFIX",
    "KnowledgeSubscription",
    "KnowledgeSubscriptions",
    "is_knowledge_principal",
    "knowledge_principal",
    "store_key",
]
