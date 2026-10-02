"""Capability lifecycle for connected-data synchronization."""

from __future__ import annotations

import logging
from typing import Any

from arcagent.modules.connected_data import _runtime
from arcagent.modules.connected_data.service import ConnectedDataService
from arcagent.tools._decorator import capability, hook

_logger = logging.getLogger("arcagent.modules.connected_data.capabilities")

# After recall (which is the query answer); the catalog is standing context.
_CATALOG_PRIORITY = 60


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
            restart_backoff_seconds=state.config.restart_backoff_seconds,
            restart_backoff_max_seconds=state.config.restart_backoff_max_seconds,
            stall_grace_seconds=state.config.stall_grace_seconds,
            failure_ceiling=state.config.consecutive_failure_ceiling,
            operator_notifier=_operator_notifier(state),
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
    lines = [
        f"- {entry.name} ({entry.kind}): status={entry.status}; homes={entry.homes_text}"
        for entry in await service.catalog_entries()
    ]
    if not lines:
        return
    prompts = ctx.data.get("prompt_source") or st.prompt_source
    preamble = prompts.resolve("arcagent", "connected_data_catalog")
    sections["connections"] = preamble + "\n" + "\n".join(lines)


#: Emitted when a connection needs a human. The module that owns the operator's
#: channel answers it and sets ``delivered``; this module names no channel.
OPERATOR_ATTENTION_EVENT = "connected_data:operator_attention"


def _operator_notifier(state: Any) -> Any:
    """Ask the module bus to tell the operator, once, that a connection needs them.

    A source dies in the background with no turn behind it, so nothing else would
    ever say so. This module owns no channel (and may not import the core that
    knows them): it emits an event, and whichever module delivers to the operator
    answers it. Never the agent's own chat. Nobody answering (a standalone agent,
    no known channel) returns False, which the service audits as undeliverable
    instead of pretending somebody was told.
    """

    async def notify(connection_id: str, reason: str) -> bool:
        bus = state.bus
        if bus is None:
            return False
        event = await bus.emit(
            OPERATOR_ATTENTION_EVENT,
            {"connection_id": connection_id, "reason": reason, "delivered": False},
        )
        return bool(event.data.get("delivered"))

    return notify


def _audit(telemetry: Any) -> Any:
    if telemetry is None:
        return None

    async def emit(action: str, payload: dict[str, Any]) -> None:
        telemetry.audit_event(action, payload)

    return emit


__all__ = [
    "OPERATOR_ATTENTION_EVENT",
    "ConnectedData",
    "inject_connections_catalog",
]
