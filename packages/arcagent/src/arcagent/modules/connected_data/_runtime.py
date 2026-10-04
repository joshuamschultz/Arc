"""Runtime state for the optional connected-data module."""

from __future__ import annotations

import contextvars
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from arcprompt import PromptSource, StockPromptSource

from arcagent.modules.connected_data.config import ConnectedDataConfig
from arcagent.modules.connected_data.ingest import (
    ArcMemoryIngestAdapter,
    ArcStoreMappingProposals,
    ArcStoreObjectState,
    ArcStoreResourceSelection,
    embedding_profile,
    memory_embedder,
)
from arcagent.modules.connected_data.service import ConnectedDataService, IngestPortFactory
from arcagent.modules.connected_data.shared import SharedKnowledge


class _State:
    def __init__(self, config: ConnectedDataConfig, **kwargs: Any) -> None:
        self.config = config
        self.workspace: Path = kwargs.get("workspace", Path("."))
        self.agent_did = str(kwargs.get("agent_did", ""))
        self.source_sync_store_opener = kwargs.get("source_sync_store_opener")
        self.arcstore_opener = kwargs.get("arcstore_opener")
        self.resource_selection_store_opener = _resource_selection_store_opener(
            self.arcstore_opener, self.agent_did
        )
        self.mapping_proposal_store_opener = _mapping_proposal_store_opener(
            self.arcstore_opener, self.agent_did
        )
        self.source_catalog = kwargs.get("source_catalog")
        #: The agent's credential broker registry (P18-2); renews before a sync reads.
        self.credential_renewals = kwargs.get("credential_renewals")
        self.telemetry = kwargs.get("telemetry")
        supplied_factory = kwargs.get("ingest_port_factory")
        #: Opens the agent's own document store: the same port the sync writes its
        #: doc pools through, so the embed backfill drains exactly that store.
        self.own_store_opener: OwnStoreOpener | None = (
            None
            if supplied_factory is not None
            else _arc_memory_own_store(
                self.workspace, self.agent_did, self.arcstore_opener, self.telemetry
            )
        )
        self.ingest_port_factory: IngestPortFactory | None = (
            supplied_factory
            if supplied_factory is not None
            else _ingest_factory(self.own_store_opener)
        )
        #: One sync and one store per connection (P18-4); only with the default
        #: ArcMemory ingest and an ArcStore to hold the subscriptions.
        self.shared_knowledge: SharedKnowledge | None = (
            _shared_knowledge(self.agent_did, self.arcstore_opener, self.telemetry)
            if supplied_factory is None and config.shared_stores
            else None
        )
        self.service: ConnectedDataService | None = None
        #: The agent's prompt lookup — the catalog preamble an operator may override.
        self.prompt_source: PromptSource = kwargs.get("prompt_source") or StockPromptSource()


_state_var: contextvars.ContextVar[_State | None] = contextvars.ContextVar(
    "arcagent_connected_data_state", default=None
)


def configure(
    *,
    config: dict[str, Any] | ConnectedDataConfig | None = None,
    workspace: Path = Path("."),
    agent_did: str = "",
    source_sync_store_opener: Any = None,
    arcstore_opener: Any = None,
    source_catalog: Any = None,
    telemetry: Any = None,
    ingest_port_factory: IngestPortFactory | None = None,
    prompt_source: PromptSource | None = None,
    credential_renewals: Any = None,
    **kwargs: Any,
) -> None:
    del kwargs
    cfg = (
        config
        if isinstance(config, ConnectedDataConfig)
        else ConnectedDataConfig(**(config or {}))
    )
    _state_var.set(
        _State(
            cfg,
            workspace=workspace,
            agent_did=agent_did,
            source_sync_store_opener=source_sync_store_opener,
            arcstore_opener=arcstore_opener,
            source_catalog=source_catalog,
            telemetry=telemetry,
            ingest_port_factory=ingest_port_factory,
            prompt_source=prompt_source,
            credential_renewals=credential_renewals,
        )
    )


def state() -> _State:
    current = _state_var.get()
    if current is None:
        raise RuntimeError("connected-data module has not been configured")
    return current


def bind(state_obj: _State) -> None:
    _state_var.set(state_obj)


def reset() -> None:
    _state_var.set(None)


#: Opens one port onto the agent's own (workspace) document store.
OwnStoreOpener = Callable[[], Awaitable[ArcMemoryIngestAdapter]]


def _arc_memory_own_store(
    workspace: Path, agent_did: str, arcstore_opener: Any, telemetry: Any
) -> OwnStoreOpener | None:
    """Compose optional ArcMemory only through the injected ArcStore seam.

    The one definition of the agent's own document store: every sync port and the
    embed backfill open it here, so they always agree on where doc pools live.
    """
    if arcstore_opener is None:
        return None

    async def open_own_store() -> ArcMemoryIngestAdapter:
        from arcagent.tools.approval_store import open_approval_store

        approval_store, backend = await open_approval_store(opener=arcstore_opener)
        return ArcMemoryIngestAdapter(
            workspace,
            agent_did,
            approval_store=approval_store,
            object_state=ArcStoreObjectState(backend, actor_did=agent_did),
            audit_sink=_audit_sink(telemetry),
        )

    return open_own_store


def _ingest_factory(open_own_store: OwnStoreOpener | None) -> IngestPortFactory | None:
    """A sync's ingest port: the agent's own store, whatever source it syncs."""
    if open_own_store is None:
        return None

    async def build(_: Any) -> ArcMemoryIngestAdapter:
        return await open_own_store()

    return build


def _shared_knowledge(
    agent_did: str, arcstore_opener: Any, telemetry: Any
) -> SharedKnowledge | None:
    """The fleet's connection-scoped stores, read and written as this agent."""
    if arcstore_opener is None or not agent_did:
        return None
    return SharedKnowledge(
        agent_did=agent_did,
        arcstore_opener=arcstore_opener,
        embedder=lambda: memory_embedder(agent_did),
        profile=lambda: embedding_profile(agent_did),
        audit_sink=_audit_sink(telemetry),
    )


def _audit_sink(telemetry: Any) -> Any:
    return getattr(telemetry, "audit_sink", None)


def _resource_selection_store_opener(arcstore_opener: Any, agent_did: str) -> Any:
    if arcstore_opener is None:
        return None

    async def open_store() -> ArcStoreResourceSelection:
        return ArcStoreResourceSelection(await arcstore_opener(), actor_did=agent_did)

    return open_store


def _mapping_proposal_store_opener(arcstore_opener: Any, agent_did: str) -> Any:
    if arcstore_opener is None:
        return None

    async def open_store() -> ArcStoreMappingProposals:
        return ArcStoreMappingProposals(await arcstore_opener(), actor_did=agent_did)

    return open_store


__all__ = ["bind", "configure", "reset", "state"]
