"""One sync and one document store per connection, read through each agent's grant (P18-4).

Every agent granted one connection used to run its own sync of it into its own
store: three agents on one GitHub account were three crawls, three copies and
three embedding bills. Now the connection has one store, written by whichever
subscribed agent holds the connection's sync lease, and every agent reads it
through its own subscription.

* **Who may read.** A :class:`Subscription` row says agent X reads connection C's
  store. It is written only after X's own approved mapping of C is verified, and
  deleted the moment X's grant is revoked, so revocation takes effect at the
  retrieval boundary with no resync. A read names the agent's own DID; a read
  for any other DID is refused (ASI03).
* **Who may write.** The store has no approval row of its own. A write is
  authorized by the writing subscriber's verified approval, re-checked against
  its subscription before every object (:class:`SubscriberAuthority`).
* **What is shared.** Only the DOCUMENT home: extracted text, its chunks and its
  vectors. Memory, profile, blob and datastore homes are an agent's own state
  (ADR-029); an agent whose mapping selects any of them keeps its own sync.
* **Vectors.** A store is embedded once, so every reader must embed queries the
  same way. The first writer claims the store's embedding profile; an agent
  configured differently keeps its own store rather than read mismatched vectors.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arctrust.paths import connected_knowledge_dir

from arcagent.connected_data import KnowledgeHome
from arcagent.modules.connected_data.ingest import ArcMemoryIngestAdapter, ArcStoreObjectState

_logger = logging.getLogger("arcagent.modules.connected_data.shared")

#: The identity a connection's shared store is filed under: in the sync store, in
#: the store's own audit events, in its document pools. Never an agent's DID.
PRINCIPAL_PREFIX = "did:arc:knowledge:"
#: The homes one store can serve to many agents. Anything else is agent state.
SHARED_HOMES = frozenset({KnowledgeHome.DOCUMENT})
_PROFILE_FILE = ".embedding-profile"


def store_key(connection_id: str) -> str:
    """A filesystem- and key-safe name for one connection's shared store."""
    return hashlib.sha256(connection_id.encode("utf-8")).hexdigest()[:32]


def knowledge_principal(connection_id: str) -> str:
    """The non-agent principal that owns one connection's sync row and store."""
    return f"{PRINCIPAL_PREFIX}{store_key(connection_id)}"


def is_knowledge_principal(did: str) -> bool:
    return did.startswith(PRINCIPAL_PREFIX)


@dataclass(frozen=True)
class Subscription:
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


class SubscriptionRegistry:
    """Durable subscriptions over ArcStore's mutable plane, keyed per agent and connection."""

    COLLECTION = "connected_knowledge_subscriptions"

    def __init__(self, backend: Any, *, actor_did: str) -> None:
        self._backend = backend
        self._actor_did = actor_did

    async def put(self, subscription: Subscription) -> None:
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

    async def get(self, agent_did: str, connection_id: str) -> Subscription | None:
        row = await self._backend.mutable_read(
            self.COLLECTION, self._key(agent_did, connection_id)
        )
        return _subscription(row)

    async def delete(self, agent_did: str, connection_id: str) -> None:
        await self._backend.mutable_delete(
            self.COLLECTION, self._key(agent_did, connection_id), actor_did=self._actor_did
        )

    async def for_agent(self, agent_did: str) -> list[Subscription]:
        rows = await self._backend.mutable_query(self.COLLECTION, where={"agent_did": agent_did})
        return _subscriptions(rows)

    async def for_connection(self, connection_id: str) -> list[Subscription]:
        rows = await self._backend.mutable_query(
            self.COLLECTION, where={"connection_id": connection_id}
        )
        return _subscriptions(rows)

    @staticmethod
    def _key(agent_did: str, connection_id: str) -> str:
        return hashlib.sha256(f"{agent_did}\0{connection_id}".encode()).hexdigest()


def _subscription(row: Any) -> Subscription | None:
    if not isinstance(row, dict):
        return None
    fields = ("agent_did", "connection_id", "source_id", "approval_id", "profile")
    values = [row.get(name) for name in fields]
    if not all(isinstance(value, str) and value for value in values):
        return None
    return Subscription(*[str(value) for value in values])


def _subscriptions(rows: list[dict[str, Any]]) -> list[Subscription]:
    parsed = (_subscription(row) for row in rows)
    return sorted(
        (row for row in parsed if row is not None),
        key=lambda row: (row.connection_id, row.agent_did),
    )


class SubscriberAuthority:
    """A writer's delegated approval, valid only while its subscription stands.

    Re-read before every write, so an agent whose grant was revoked mid-run stops
    writing the shared store at the next object.
    """

    def __init__(
        self, registry: SubscriptionRegistry, agent_did: str, connection_id: str, approval_id: str
    ) -> None:
        self._registry = registry
        self._agent_did = agent_did
        self._connection_id = connection_id
        self._approval_id = approval_id

    async def authorized_homes(self) -> tuple[str, tuple[KnowledgeHome, ...]] | None:
        current = await self._registry.get(self._agent_did, self._connection_id)
        if current is None or current.approval_id != self._approval_id:
            return None
        return self._approval_id, tuple(SHARED_HOMES)


class _ReadOnly:
    """The authority of a reader's port: it authorizes no write at all."""

    async def authorized_homes(self) -> tuple[str, tuple[KnowledgeHome, ...]] | None:
        return None


class SharedKnowledge:
    """Ports onto the fleet's connection-scoped stores, for one agent."""

    def __init__(
        self,
        *,
        agent_did: str,
        arcstore_opener: Callable[[], Awaitable[Any]],
        embedder: Callable[[], Any | None],
        profile: Callable[[], str],
        audit_sink: Any | None = None,
        root: Callable[[], Path] = connected_knowledge_dir,
    ) -> None:
        self.agent_did = agent_did
        self._arcstore_opener = arcstore_opener
        self._build_embedder = embedder
        # Built once: every read embeds its query, and loading a model per read
        # would put the model's start-up cost on every turn.
        self._embedder: Any | None = None
        self._embedder_built = False
        self._profile = profile
        self._audit_sink = audit_sink
        self._root = root

    def root(self, connection_id: str) -> Path:
        """Where one connection's shared store lives (resolved per call)."""
        return self._root() / store_key(connection_id)

    def profile(self) -> str:
        return self._profile()

    async def registry(self) -> SubscriptionRegistry:
        return SubscriptionRegistry(await self._arcstore_opener(), actor_did=self.agent_did)

    async def writer(self, connection_id: str, approval_id: str) -> ArcMemoryIngestAdapter:
        """A port that writes the store under this agent's verified approval."""
        registry = await self.registry()
        authority = SubscriberAuthority(registry, self.agent_did, connection_id, approval_id)
        return await self._port(connection_id, authority)

    async def reader(self, connection_id: str) -> ArcMemoryIngestAdapter:
        """A port that reads the store and can write nothing."""
        return await self._port(connection_id, _ReadOnly())

    async def _port(self, connection_id: str, authority: Any) -> ArcMemoryIngestAdapter:
        principal = knowledge_principal(connection_id)
        return ArcMemoryIngestAdapter(
            self.root(connection_id),
            principal,
            approval_store=None,
            object_state=ArcStoreObjectState(await self._arcstore_opener(), actor_did=principal),
            embedder=self._embedder_once(),
            audit_sink=self._audit_sink,
            authority=authority,
        )

    def _embedder_once(self) -> Any | None:
        if not self._embedder_built:
            self._embedder = self._build_embedder()
            self._embedder_built = True
        return self._embedder

    def claim_profile(self, connection_id: str) -> bool:
        """True when this agent embeds the way the store was (or is now first) embedded.

        The first claim creates the marker exclusively, so two first writers on one
        host cannot both win; every later claim compares.
        """
        root = self.root(connection_id)
        marker = root / _PROFILE_FILE
        mine = self._profile()
        try:
            root.mkdir(parents=True, exist_ok=True)
            with marker.open("x", encoding="utf-8") as handle:
                handle.write(mine)
            return True
        except FileExistsError:
            try:
                return marker.read_text(encoding="utf-8").strip() == mine
            except OSError:
                return False
        except OSError:
            _logger.warning("shared knowledge store unwritable: %s", root)
            return False


__all__ = [
    "PRINCIPAL_PREFIX",
    "SHARED_HOMES",
    "SharedKnowledge",
    "SubscriberAuthority",
    "Subscription",
    "SubscriptionRegistry",
    "is_knowledge_principal",
    "knowledge_principal",
    "store_key",
]
