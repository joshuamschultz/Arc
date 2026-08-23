"""Runtime state for the optional connected-data module."""

from __future__ import annotations

import contextvars
from pathlib import Path
from typing import Any

from arcagent.modules.connected_data.config import ConnectedDataConfig
from arcagent.modules.connected_data.service import ConnectedDataService, IngestPortFactory


class _State:
    def __init__(self, config: ConnectedDataConfig, **kwargs: Any) -> None:
        self.config = config
        self.workspace: Path = kwargs.get("workspace", Path("."))
        self.agent_did = str(kwargs.get("agent_did", ""))
        self.arcstore_opener = kwargs.get("arcstore_opener")
        self.source_catalog = kwargs.get("source_catalog")
        self.ingest_port_factory: IngestPortFactory | None = kwargs.get("ingest_port_factory")
        self.telemetry = kwargs.get("telemetry")
        self.service: ConnectedDataService | None = None


_state_var: contextvars.ContextVar[_State | None] = contextvars.ContextVar(
    "arcagent_connected_data_state", default=None
)


def configure(
    *,
    config: dict[str, Any] | ConnectedDataConfig | None = None,
    workspace: Path = Path("."),
    agent_did: str = "",
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


__all__ = ["bind", "configure", "reset", "state"]
