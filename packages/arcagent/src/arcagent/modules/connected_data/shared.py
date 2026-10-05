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
  its subscription before every object (checked in the sync worker, which
  writes every store; this process only reads them).
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
from arcagent.modules.connected_data.sync_worker.remote_port import (
    RemoteIngestPort,
    WriterChannel,
    seal_key_for,
)
from arcagent.modules.connected_data.sync_worker.specs import Authority, StoreSpec
from arcagent.modules.connected_data.sync_worker.supervisor import process_supervisor

_logger = logging.getLogger("arcagent.modules.connected_data.shared")

#: The homes one store can serve to many agents. Anything else is agent state.
SHARED_HOMES = frozenset({KnowledgeHome.DOCUMENT})
_PROFILE_FILE = ".embedding-profile"


def default_channel() -> WriterChannel:
    """This process's supervised sync worker."""
    return process_supervisor().channel()


class _ReadOnly:
    """The authority of a reader's port: it authorizes no write at all."""

    async def authorized_homes(self) -> tuple[str, tuple[KnowledgeHome, ...]] | None:
        return None


class SharedKnowledge:
    """Ports onto the fleet's connection-scoped stores, for one agent.

    A port reads its store here, read-only, and writes it through the sync
    worker (``sync_worker.RemoteIngestPort``): this process never opens a write
    connection to a shared store.
    """

    def __init__(
        self,
        *,
        agent_did: str,
        arcstore_opener: Callable[[], Awaitable[Any]],
        embedder: Callable[[], Any | None],
        profile: Callable[[], str],
        embed: Callable[[], tuple[str, str, str] | None],
        channel: Callable[[], WriterChannel] = default_channel,
        audit_sink: Any | None = None,
    ) -> None:
        self.agent_did = agent_did
        self._arcstore_opener = arcstore_opener
        self._build_embedder = embedder
        # Built once: every read embeds its query, and loading a model per read
        # would put the model's start-up cost on every turn.
        self._embedder: Any | None = None
        self._embedder_built = False
        self._profile = profile
        self._embed = embed
        self._channel = channel
        self._audit_sink = audit_sink
        #: The release of each store root's pinned seal key, per root this agent opened.
        self._held: dict[Path, Callable[[], None]] = {}

    def root(self, connection_id: str) -> Path:
        """Where one connection's shared store lives (resolved per call)."""
        return connected_knowledge_dir() / store_key(connection_id)

    def profile(self) -> str:
        return self._profile()

    async def registry(self) -> KnowledgeSubscriptions:
        return KnowledgeSubscriptions(await self._arcstore_opener(), actor_did=self.agent_did)

    async def writer(self, connection_id: str, approval_id: str) -> RemoteIngestPort:
        """A port that writes the store under this agent's live subscription."""
        return await self._remote(connection_id, "subscriber", approval_id)

    async def migration_writer(self, connection_id: str, approval_id: str) -> RemoteIngestPort:
        """A port that adopts this agent's own store under the approval it just verified.

        Used before the agent subscribes, so the store is whole before it is read.
        """
        return await self._remote(connection_id, "migration", approval_id)

    async def orphan(self, connection_id: str) -> RemoteIngestPort:
        """A port that can only purge a store no agent reads any more."""
        return await self._remote(connection_id, "orphan", "")

    async def reader(self, connection_id: str) -> ArcMemoryIngestAdapter:
        """A port that reads the store and can write nothing."""
        return await self._local(connection_id, _ReadOnly())

    async def claim_profile(self, connection_id: str, approval_id: str) -> bool:
        """True when this agent embeds the way the store was (or is now first) embedded.

        The first claim writes the store's marker (in the worker) exclusively, so
        two first writers cannot both win; every later claim compares.
        """
        port = await self._remote(connection_id, "migration", approval_id)
        try:
            return await port.claim_profile(self._profile())
        finally:
            await port.aclose()

    async def _remote(
        self, connection_id: str, authority: Authority, approval_id: str
    ) -> RemoteIngestPort:
        local = await self._local(connection_id, _ReadOnly())
        spec = StoreSpec(
            kind="shared",
            agent_did=self.agent_did,
            root=str(self.root(connection_id)),
            authority=authority,
            connection_id=connection_id,
            approval_id=approval_id,
            seal=seal_key_for(self.root(connection_id)),
            embed=self._embed(),
        )
        return RemoteIngestPort(local, spec, self._channel(), audit_sink=self._audit_sink)

    async def _local(self, connection_id: str, authority: Any) -> ArcMemoryIngestAdapter:
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
            durability="normal",
            read_only=True,
        )

    async def _hold_seal_key(self, connection_id: str, principal: str) -> None:
        """Pin the store principal's key for the store root (once per root per agent).

        Readers verify the store's seal with it, and the sync worker's writes are
        signed with it here, by request: the key never leaves this process.
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


def claim_store_profile(root: Path, profile: str) -> bool:
    """Claim a shared store's embedding profile, or compare against the claim made.

    Runs in the sync worker, the store's only writer. The marker is created
    exclusively, so two first writers on one host cannot both win.
    """
    marker = root / _PROFILE_FILE
    try:
        root.mkdir(parents=True, exist_ok=True)
        with marker.open("x", encoding="utf-8") as handle:
            handle.write(profile)
        return True
    except FileExistsError:
        try:
            return marker.read_text(encoding="utf-8").strip() == profile
        except OSError:
            return False
    except OSError:
        _logger.warning("shared knowledge store unwritable: %s", root)
        return False


__all__ = [
    "SHARED_HOMES",
    "SharedKnowledge",
    "claim_store_profile",
    "default_channel",
]
