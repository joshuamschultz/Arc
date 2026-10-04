"""Capability lifecycle for connected-data synchronization."""

from __future__ import annotations

import logging
from typing import Any

from arcagent.extension.connection_health import StoreHealthReporter
from arcagent.modules.connected_data import _runtime
from arcagent.modules.connected_data.service import CatalogEntry, ConnectedDataService
from arcagent.tools._decorator import capability, hook

_logger = logging.getLogger("arcagent.modules.connected_data.capabilities")

# After recall (which is the query answer); the catalog is standing context.
_CATALOG_PRIORITY = 60


@capability(name="connected_data", depends_on=("connectors",))
class ConnectedData:
    """Start source synchronization only when all optional seams are present.

    Starts after ``connectors``, which fills the source catalog. The catalog's
    change signal still wakes the monitor for a source attached later; the order
    only means the first tick already sees what is attached at boot.
    """

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
            restart_backoff_seconds=state.config.restart_backoff_seconds,
            restart_backoff_max_seconds=state.config.restart_backoff_max_seconds,
            stall_grace_seconds=state.config.stall_grace_seconds,
            failure_ceiling=state.config.consecutive_failure_ceiling,
            health=_health_reporter(state),
            renewals=state.credential_renewals,
            shared=state.shared_knowledge,
            own_store_opener=state.own_store_opener,
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


@hook(event="agent:assemble_prompt", priority=_CATALOG_PRIORITY)
async def inject_connections_catalog(ctx: Any) -> None:
    """Surface the connected-knowledge catalog so the agent knows to search it.

    A lean, always-cheap manifest (store reads only, never a live adapter call):
    each connected source's name, kind, sync status and memory homes, plus one
    nudge toward the search tools. Owned by THIS module, so a deployment with no
    connectors injects nothing and the prompt surface stays clean.
    """
    try:
        st = _runtime.state()
    except RuntimeError:
        return
    service = st.service
    if service is None:
        return
    sections = ctx.data.get("sections")
    if not isinstance(sections, dict):
        return
    lines = [_catalog_line(entry) for entry in await service.catalog_entries()]
    if not lines:
        return
    prompts = ctx.data.get("prompt_source") or st.prompt_source
    preamble = prompts.resolve("arcagent", "connected_data_catalog")
    sections["connections"] = preamble + "\n" + "\n".join(lines)


def _catalog_line(entry: CatalogEntry) -> str:
    """One source's catalog line, plus a short preview when the operator wrote a guide.

    Only a preview: the full guide reaches the agent when a tool touches the
    source, once per run, so standing context stays lean.
    """
    line = f"- {entry.name} ({entry.kind}): status={entry.status}; homes={entry.homes_text}"
    if entry.guide:
        line += f"\n  operator guide: {entry.guide}"
    return line


def _health_reporter(state: Any) -> StoreHealthReporter | None:
    """Report sync outcomes to the shared connection-health record.

    The deployment's health authority decides what a report means and tells the
    operator once per outage; this module names no channel and sends nothing. No
    arcstore (a standalone agent) means no shared record, so no reporter: the
    local backoff still protects a dead credential.
    """
    if state.arcstore_opener is None:
        return None
    sink = getattr(state.telemetry, "audit_sink", None)
    return StoreHealthReporter(state.arcstore_opener, sink=sink)


def _audit(telemetry: Any) -> Any:
    if telemetry is None:
        return None

    async def emit(action: str, payload: dict[str, Any]) -> None:
        telemetry.audit_event(action, payload)

    return emit


__all__ = [
    "ConnectedData",
    "inject_connections_catalog",
]
