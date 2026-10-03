"""One sync and one document store per connection, read through each agent's grant (P18-4).

Every agent granted one connection used to run its own sync of it into its own
store: three agents on one GitHub account were three crawls, three copies and
three embedding bills. Now the connection has one store, written by whichever
subscribed agent holds the connection's sync lease, and every agent reads it
through its own subscription.

* **Who may read.** A :class:`KnowledgeSubscription` row says agent X reads connection C's
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
* **Signed index.** The store's ``index.md`` / ``log.md`` seal is signed by the
  connection's knowledge principal, never an agent: its key is a capability
  arctrust custody resolves (:func:`arctrust.knowledge_signer_for`), pinned for
  the store's root while this agent has it open and released at :meth:`close`.
  Without a custody key nothing is signed and readers trust nothing (fail closed).
* **Vectors.** A store is embedded once, so every reader must embed queries the
  same way. The first writer claims the store's embedding profile; an agent
  configured differently keeps its own store rather than read mismatched vectors.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from importlib import import_module
from pathlib import Path
from typing import Any

from arctrust import knowledge_signer_for
from arctrust.paths import connected_knowledge_dir

from arcagent.connected_data import KnowledgeHome
from arcagent.extension.knowledge_subscriptions import (
    KnowledgeSubscriptions,
    knowledge_principal,
    store_key,
)
from arcagent.modules.connected_data.ingest import ArcMemoryIngestAdapter, ArcStoreObjectState

_logger = logging.getLogger("arcagent.modules.connected_data.shared")

#: The homes one store can serve to many agents. Anything else is agent state.
SHARED_HOMES = frozenset({KnowledgeHome.DOCUMENT})
_PROFILE_FILE = ".embedding-profile"


class SubscriberAuthority:
    """A writer's delegated approval, valid only while its subscription stands.

    Re-read before every write, so an agent whose grant was revoked mid-run stops
    writing the shared store at the next object.
    """

    def __init__(
        self,
        registry: KnowledgeSubscriptions,
        agent_did: str,
        connection_id: str,
        approval_id: str,
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


class _VerifiedNow:
    """A migrating agent's own approval, verified moments before it subscribes."""

    def __init__(self, approval_id: str) -> None:
        self._approval_id = approval_id

    async def authorized_homes(self) -> tuple[str, tuple[KnowledgeHome, ...]] | None:
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
        #: The release of each store root's pinned seal key, per root this agent opened.
        self._held: dict[Path, Callable[[], None]] = {}

    def root(self, connection_id: str) -> Path:
        """Where one connection's shared store lives (resolved per call)."""
        return self._root() / store_key(connection_id)

    def profile(self) -> str:
        return self._profile()

    async def registry(self) -> KnowledgeSubscriptions:
        return KnowledgeSubscriptions(await self._arcstore_opener(), actor_did=self.agent_did)

    async def writer(self, connection_id: str, approval_id: str) -> ArcMemoryIngestAdapter:
        """A port that writes the store under this agent's verified approval."""
        registry = await self.registry()
        authority = SubscriberAuthority(registry, self.agent_did, connection_id, approval_id)
        return await self._port(connection_id, authority)

    async def migration_writer(
        self, connection_id: str, approval_id: str
    ) -> ArcMemoryIngestAdapter:
        """A port that adopts this agent's own store under the approval it just verified.

        Used before the agent subscribes, so the store is whole before it is read.
        """
        return await self._port(connection_id, _VerifiedNow(approval_id))

    async def reader(self, connection_id: str) -> ArcMemoryIngestAdapter:
        """A port that reads the store and can write nothing."""
        return await self._port(connection_id, _ReadOnly())

    async def _port(self, connection_id: str, authority: Any) -> ArcMemoryIngestAdapter:
        principal = knowledge_principal(connection_id)
        await self._hold_seal_key(connection_id, principal)
        return ArcMemoryIngestAdapter(
            self.root(connection_id),
            principal,
            approval_store=None,
            object_state=ArcStoreObjectState(await self._arcstore_opener(), actor_did=principal),
            embedder=self._embedder_once(),
            audit_sink=self._audit_sink,
            authority=authority,
        )

    async def _hold_seal_key(self, connection_id: str, principal: str) -> None:
        """Pin the store principal's key for the store root (once per root per agent).

        Held for as long as this agent has the store open, not per port: the
        store's folder listings are re-indexed by a debounced drain that runs
        after a sync's port has closed, and it must still be able to sign.
        """
        root = self.root(connection_id).resolve()
        if root in self._held:
            return
        try:
            seal = import_module("arcmemory.okf_seal")
        except ImportError:  # no memory extra: this agent writes and reads no index
            return
        signer = await asyncio.to_thread(knowledge_signer_for, principal)
        if signer is None or root in self._held:
            return
        self._held[root] = seal.hold_memory_identity(root, signer)

    def close(self) -> None:
        """Release every store key this agent pinned (module teardown)."""
        held, self._held = self._held, {}
        for release in held.values():
            release()

    def _embedder_once(self) -> Any | None:
        if not self._embedder_built:
            self._embedder = self._build_embedder()
            self._embedder_built = True
        return self._embedder

    def profile_compatible(self, connection_id: str) -> bool:
        """Whether this agent could read the store; claims nothing (a dry run)."""
        marker = self.root(connection_id) / _PROFILE_FILE
        try:
            return marker.read_text(encoding="utf-8").strip() == self._profile()
        except FileNotFoundError:
            return True
        except OSError:
            return False

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
    "SHARED_HOMES",
    "SharedKnowledge",
    "SubscriberAuthority",
]
