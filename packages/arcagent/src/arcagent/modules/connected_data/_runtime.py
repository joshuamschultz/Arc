"""Runtime state for the optional connected-data module."""

from __future__ import annotations

import contextvars
from pathlib import Path
from typing import Any

from arcagent.modules.connected_data.config import ConnectedDataConfig
from arcagent.modules.connected_data.ingest import (
    ArcMemoryIngestAdapter,
    ArcStoreObjectState,
    ArcStoreResourceSelection,
)
from arcagent.modules.connected_data.service import ConnectedDataService, IngestPortFactory


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
        self.source_catalog = kwargs.get("source_catalog")
        self.telemetry = kwargs.get("telemetry")
        supplied_factory = kwargs.get("ingest_port_factory")
        self.ingest_port_factory: IngestPortFactory | None = (
            supplied_factory
            if supplied_factory is not None
            else _arc_memory_ingest_factory(
                self.workspace,
                self.agent_did,
                self.arcstore_opener,
                self.telemetry,
            )
        )
        self.service: ConnectedDataService | None = None


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


def _arc_memory_ingest_factory(
    workspace: Path, agent_did: str, arcstore_opener: Any, telemetry: Any
) -> IngestPortFactory | None:
    """Compose optional ArcMemory only through the injected ArcStore seam."""
    if arcstore_opener is None:
        return None

    async def build(_: Any) -> ArcMemoryIngestAdapter:
        from arcagent.tools.approval_store import open_approval_store

        approval_store, backend = await open_approval_store(opener=arcstore_opener)
        return ArcMemoryIngestAdapter(
            workspace,
            agent_did,
            approval_store=approval_store,
            object_state=ArcStoreObjectState(backend, actor_did=agent_did),
            audit_sink=_audit_sink(telemetry),
        )

    return build


def _audit_sink(telemetry: Any) -> Any:
    return getattr(telemetry, "audit_sink", None)


def _resource_selection_store_opener(arcstore_opener: Any, agent_did: str) -> Any:
    if arcstore_opener is None:
        return None

    async def open_store() -> ArcStoreResourceSelection:
        return ArcStoreResourceSelection(await arcstore_opener(), actor_did=agent_did)

    return open_store


__all__ = ["bind", "configure", "reset", "state"]
