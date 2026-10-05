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
    embed_settings,
    embedding_profile,
    memory_embedder,
)
from arcagent.modules.connected_data.service import ConnectedDataService, IngestPortFactory
from arcagent.modules.connected_data.shared import SharedKnowledge, default_channel
from arcagent.modules.connected_data.sync_worker import (
    RemoteIngestPort,
    StoreSpec,
    process_supervisor,
)
from arcagent.modules.connected_data.sync_worker.remote_port import seal_key_for


class _State:
    def __init__(self, config: ConnectedDataConfig, **kwargs: Any) -> None:
        self.config = config
        self.workspace: Path = kwargs.get("workspace", Path("."))
        self.config_path: Path = kwargs.get("config_path", Path("arcagent.toml"))
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
        #: The agent's own document store (its workspace), read here and written
        #: by the sync worker. ``None`` without arcstore or with a supplied factory.
        self.own_store_opener: Callable[[], Awaitable[RemoteIngestPort]] | None = (
            None
            if supplied_factory is not None
            else _own_store_opener(
                self.workspace,
                self.config_path,
                self.agent_did,
                self.arcstore_opener,
                self.telemetry,
            )
        )
        own = self.own_store_opener
        self.ingest_port_factory: IngestPortFactory | None = (
            supplied_factory if own is None else _factory_of(own)
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
    config_path: Path = Path("arcagent.toml"),
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
    if arcstore_opener is not None and agent_did:
        # The sync worker writes this agent's stores; this process answers its
        # arcstore reads and signs its seals, for the agents it serves only.
        host = process_supervisor().host
        host.serve_agent(agent_did)
        host.bind(arcstore_opener=arcstore_opener, audit_sink=_audit_sink(telemetry))
    _state_var.set(
        _State(
            cfg,
            workspace=workspace,
            config_path=config_path,
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


def _own_store_opener(
    workspace: Path, config_path: Path, agent_did: str, arcstore_opener: Any, telemetry: Any
) -> Callable[[], Awaitable[RemoteIngestPort]] | None:
    """Compose optional ArcMemory only through the injected ArcStore seam.

    The port reads the agent's own store here, read-only, and every write goes
    to the sync worker, which checks that ``workspace`` is the workspace the
    agent's own ``arcagent.toml`` names.
    """
    if arcstore_opener is None:
        return None

    async def open_port() -> RemoteIngestPort:
        from arcagent.tools.approval_store import open_approval_store

        approval_store, backend = await open_approval_store(opener=arcstore_opener)
        sink = _audit_sink(telemetry)
        local = ArcMemoryIngestAdapter(
            workspace,
            agent_did,
            approval_store=approval_store,
            object_state=ArcStoreObjectState(backend, actor_did=agent_did),
            audit_sink=sink,
            read_only=True,
        )
        store = StoreSpec(
            kind="own",
            agent_did=agent_did,
            root=str(Path(workspace).resolve()),
            authority="owner",
            config_path=str(Path(config_path).resolve()),
            seal=seal_key_for(Path(workspace)),
            embed=embed_settings(agent_did),
        )
        return RemoteIngestPort(local, store, default_channel(), audit_sink=sink)

    return open_port


def _factory_of(open_port: Callable[[], Awaitable[RemoteIngestPort]]) -> IngestPortFactory:
    """The agent's own store, whichever source it is asked for (one store per agent)."""

    async def build(_: Any) -> RemoteIngestPort:
        return await open_port()

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
        embed=lambda: embed_settings(agent_did),
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
