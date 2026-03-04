"""UI Reporter module — bridges agent events to ArcUI dashboard.

Subscribes to ModuleBus events, wraps them as UIEvent-compatible JSON,
and streams to an ArcUI server via WebSocket. Receives control messages
from the UI and re-emits them on the bus as ``ui:control`` events.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from arcagent.core.module_bus import EventContext, ModuleContext

_logger = logging.getLogger("arcagent.ui_reporter")

# Events from arcrun bridged as agent:pre_tool/post_tool etc.
# These map to UIEvent layer="run", not "agent".
_RUN_LAYER_SUFFIXES = frozenset({
    "pre_tool",
    "post_tool",
    "pre_plan",
    "post_plan",
})

# Known ModuleBus events to subscribe to.
_LLM_EVENTS = (
    "llm:call_complete",
    "llm:config_change",
    "llm:circuit_change",
)

_AGENT_EVENTS = (
    "agent:init",
    "agent:ready",
    "agent:shutdown",
    "agent:pre_respond",
    "agent:post_respond",
    "agent:error",
    "agent:extensions_loaded",
    "agent:skills_loaded",
    "agent:tools_reloaded",
    "agent:pre_tool",
    "agent:post_tool",
    "agent:pre_plan",
    "agent:post_plan",
    "agent:pre_compaction",
)


class UIReporterConfig(BaseModel):
    """Configuration for the UI reporter module."""

    enabled: bool = False
    url: str = "ws://localhost:8420/api/agent/connect"
    token: str = ""
    reconnect_max_interval: float = Field(default=60.0, gt=0)
    buffer_size: int = Field(default=1000, ge=1)


class UIReporterModule:
    """UI reporter — Module Bus participant.

    Wraps internal events as UIEvent-compatible payloads and streams
    them to the ArcUI server. Observational priority (200) ensures
    business logic runs first.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        workspace: Path = Path("."),
        transport: Any | None = None,
        **_kw: Any,
    ) -> None:
        self._config = UIReporterConfig(**(config or {}))
        self._workspace = workspace
        self._transport = transport
        self._sequence = 0
        self._agent_name = ""
        self._agent_id = ""
        self._source_id = ""

    @property
    def name(self) -> str:
        return "ui_reporter"

    async def startup(self, ctx: ModuleContext) -> None:
        """Subscribe to bus events for forwarding to UI."""
        if not self._config.enabled:
            _logger.info("UI reporter disabled, skipping startup")
            return

        self._agent_name = ctx.config.agent.name
        self._agent_id = getattr(ctx.config.agent, "did", "") or self._agent_name
        self._source_id = getattr(ctx.config.agent, "did", "")

        # Create transport if not injected and config is valid
        if self._transport is None and self._config.token:
            try:
                from arcui.transport_ws import WebSocketTransport

                self._transport = WebSocketTransport(
                    url=self._config.url,
                    token=self._config.token,
                    reconnect_cap=self._config.reconnect_max_interval,
                    buffer_size=self._config.buffer_size,
                )
            except ImportError:
                _logger.warning("arcui not installed, transport disabled")

        # Subscribe to LLM events
        for event in _LLM_EVENTS:
            ctx.bus.subscribe(
                event,
                self._on_event,
                priority=200,
                module_name="ui_reporter",
            )

        # Subscribe to agent/run events
        for event in _AGENT_EVENTS:
            ctx.bus.subscribe(
                event,
                self._on_event,
                priority=200,
                module_name="ui_reporter",
            )

        _logger.info("UI reporter started, target=%s", self._config.url)

    async def shutdown(self) -> None:
        """Clean up resources."""
        if self._transport is not None:
            try:
                await self._transport.close()
            except Exception:
                _logger.debug("Error closing transport", exc_info=True)
        _logger.info("UI reporter shut down")

    async def _on_event(self, ctx: EventContext) -> None:
        """Handle any subscribed bus event — wrap and forward to UI."""
        payload = self._wrap_event(ctx.event, ctx.data)
        _logger.debug("UI event: %s → layer=%s", ctx.event, payload["layer"])

        # Send via transport if available
        if self._transport is not None:
            try:
                from arcui.types import UIEvent

                event = UIEvent(**payload)
                await self._transport.send_event(self._agent_id, event)
            except Exception:
                _logger.debug("Failed to send event via transport", exc_info=True)

    def _wrap_event(
        self, event: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Convert a ModuleBus event into a UIEvent-compatible dict.

        Returns a flat dict matching the UIEvent Pydantic model schema.
        """
        layer = self._classify_layer(event)
        # Extract event_type: everything after the first colon
        event_type = event.split(":", 1)[1] if ":" in event else event

        seq = self._sequence
        self._sequence += 1

        return {
            "layer": layer,
            "event_type": event_type,
            "agent_id": self._agent_id,
            "agent_name": self._agent_name,
            "source_id": self._source_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "data": dict(data),
            "sequence": seq,
        }

    @staticmethod
    def _classify_layer(event: str) -> str:
        """Map a ModuleBus event name to a UIEvent layer.

        - ``llm:*`` → ``"llm"``
        - ``agent:pre_tool``, ``agent:post_tool``, ``agent:pre_plan``,
          ``agent:post_plan`` → ``"run"`` (arcrun bridge events)
        - All other ``agent:*`` → ``"agent"``
        """
        if event.startswith("llm:"):
            return "llm"
        if event.startswith("agent:"):
            suffix = event.split(":", 1)[1]
            if suffix in _RUN_LAYER_SUFFIXES:
                return "run"
            return "agent"
        # Future: team:* → "team"
        return "agent"


__all__ = ["UIReporterConfig", "UIReporterModule"]
