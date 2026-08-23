"""Capability lifecycle for connected-data synchronization."""

from __future__ import annotations

from typing import Any

from arcagent.modules.connected_data import _runtime
from arcagent.modules.connected_data.service import ConnectedDataService
from arcagent.tools._decorator import capability


@capability(name="connected_data")
class ConnectedData:
    """Start source synchronization only when all optional seams are present."""

    def __init__(self) -> None:
        self._service: ConnectedDataService | None = None

    async def setup(self, ctx: Any) -> None:
        del ctx
        state = _runtime.state()
        if state.source_catalog is None:
            return
        state.service = ConnectedDataService(
            state.source_catalog,
            agent_did=state.agent_did,
            sync_store_opener=state.source_sync_store_opener,
            ingest_factory=state.ingest_port_factory,
            resource_selection_store_opener=state.resource_selection_store_opener,
            mapping_proposal_store_opener=state.mapping_proposal_store_opener,
            limits=state.config.limits,
            global_concurrency=state.config.global_concurrency,
            audit=_audit(state.telemetry),
            interval_seconds=state.config.interval_seconds,
        )
        await state.service.start()
        self._service = state.service

    async def teardown(self) -> None:
        service = _runtime.state().service
        if service is not None:
            await service.close()
        _runtime.state().service = None
        self._service = None

    @property
    def service(self) -> ConnectedDataService | None:
        """The live service for authenticated operator control surfaces."""
        return self._service


def _audit(telemetry: Any) -> Any:
    if telemetry is None:
        return None

    async def emit(action: str, payload: dict[str, Any]) -> None:
        telemetry.audit_event(action, payload)

    return emit


__all__ = ["ConnectedData"]
