"""SPEC-082 T-1112 / SPEC-084 T-1146 — assemble a serving door from an agent.

arcagent stays headless — it never binds a port. A CLI (or the arcui mount) serves
the door, but first it must ASSEMBLE the door from a started agent.
:func:`build_door_from_agent` is that factory: it reads only the agent's public
surface (tool registry, DID, tier, MCP config, audit sink) and wires the door's
collaborators — :class:`~arcagent.modules.mcp_server.server.McpServer`, the
tier-checked :class:`~arcagent.modules.mcp_server.allowlist.ExposureAllowlist`, an
:class:`~arcagent.capabilities.provider.AgentCapabilityProvider`, a
:class:`~arcteam.crypto.ReplayCache`, and the agent's audit sink — into the door's
``mcp`` SDK :class:`~mcp.server.lowlevel.Server` (which stdio serving drives) and its
:class:`~arcagent.modules.mcp_server.http_transport.HttpDoor` ASGI app.

A door is a default-off, security-sensitive surface, so the factory REFUSES
(raises ``ValueError``) when ``[modules.mcp_server]`` is disabled — a door must
never serve when the operator did not enable it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, cast

import arcrun
from arcteam.crypto import ReplayCache
from arctrust import AuditSink
from mcp.server.lowlevel import Server

from arcagent.capabilities.provider import AgentCapabilityProvider
from arcagent.modules.mcp_server.allowlist import ExposureAllowlist
from arcagent.modules.mcp_server.config import McpServerConfig
from arcagent.modules.mcp_server.http_transport import HttpDoor
from arcagent.modules.mcp_server.sdk_server import build_sdk_server
from arcagent.modules.mcp_server.server import McpServer


class _Registry(Protocol):
    """The tool-registry surface the factory reads (``ToolRegistry``-shaped)."""

    @property
    def tools(self) -> dict[str, Any]: ...


class _Agent(Protocol):
    """The public surface :func:`build_door_from_agent` consumes from a started agent."""

    @property
    def tool_registry(self) -> _Registry: ...

    @property
    def did(self) -> str: ...

    @property
    def tier(self) -> str: ...

    @property
    def mcp_config(self) -> McpServerConfig: ...

    @property
    def audit_sink(self) -> AuditSink: ...


@dataclass(frozen=True)
class BuiltDoor:
    """The assembled door.

    ``server`` is the ``mcp`` SDK low-level server that the stdio transport (and an
    in-memory client session) drives; ``http_app`` is the ASGI door the HTTP
    transport serves. Both run the SAME verify → authorize → dispatch → audit
    pipeline over the same collaborators.
    """

    server: Server[Any, Any]
    http_app: HttpDoor


def build_door_from_agent(agent: _Agent) -> BuiltDoor:
    """Assemble a serving door from a started agent. Refuses a disabled config.

    Raises:
        ValueError: when ``[modules.mcp_server]`` is disabled — a door must never
            serve unless the operator enabled it.
    """
    config = agent.mcp_config
    if not config.enabled:
        raise ValueError(
            "the MCP door is disabled — enable [modules.mcp_server] before serving it"
        )

    tier = agent.tier
    registry = agent.tool_registry
    server = McpServer(registry, server_name=config.server_name, page_size=config.page_size)  # type: ignore[arg-type]
    allowlist = ExposureAllowlist.from_config(config, tier)
    provider = AgentCapabilityProvider(
        tools=_arcrun_tools(registry),
        skills=[],
        tier=tier,
        caller_did=agent.did,
    )
    replay_cache = ReplayCache()
    audit_sink = agent.audit_sink

    common: dict[str, Any] = {
        "server_name": config.server_name,
        "tier": tier,
        "provider": provider,
        "allowlist": allowlist,
        "replay_cache": replay_cache,
        "audit_sink": audit_sink,
        "enrolled": config.enrolled,
    }
    sdk_server = build_sdk_server(server, **common)
    http_app = HttpDoor(server, **common)
    return BuiltDoor(server=sdk_server, http_app=http_app)


class _StartedAgentView:
    """Adapts a started :class:`arcagent.ArcAgent` to the door-serving surface.

    A started agent exposes ``did`` and ``audit_sink`` publicly but keeps its tool
    registry, tier, and module config internal; this within-package adapter reads
    them and presents the ``tool_registry`` / ``tier`` / ``mcp_config`` trio
    :func:`build_door_from_agent` needs — so the MCP config never has to live on
    the agent nucleus (``test_mcp_server_absence``).
    """

    def __init__(self, agent: Any) -> None:
        self._agent = agent

    @property
    def tool_registry(self) -> Any:
        return self._agent._tool_registry

    @property
    def did(self) -> str:
        return str(self._agent.did)

    @property
    def audit_sink(self) -> AuditSink:
        return cast(AuditSink, self._agent.audit_sink)

    @property
    def tier(self) -> str:
        return str(self._agent._config.security.tier)

    @property
    def mcp_config(self) -> McpServerConfig:
        entry = self._agent._config.modules.get("mcp_server")
        if entry is None:
            return McpServerConfig(enabled=False)
        # The module toggle (`[modules.mcp_server] enabled`) is authoritative for
        # whether the door serves; it overrides any `enabled` the rendered
        # `[modules.mcp_server.config]` table also carries (the scaffold/overlay
        # writes the full McpServerConfig defaults, `enabled` among them, so
        # passing both as keywords would collide — TypeError).
        return McpServerConfig(**{**entry.config, "enabled": entry.enabled})


def build_door_from_started_agent(agent: Any) -> BuiltDoor:
    """Assemble a serving door from a started :class:`arcagent.ArcAgent`.

    Adapts the real agent to the door surface and defers to
    :func:`build_door_from_agent`; refuses (``ValueError``) a disabled door.
    """
    return build_door_from_agent(_StartedAgentView(agent))


def _arcrun_tools(registry: _Registry) -> list[arcrun.Tool]:
    """The registry's tools as arcrun ``Tool`` objects.

    A real ``ToolRegistry`` maps its wrapped ``RegisteredTool`` entries to arcrun
    tools via ``to_arcrun_tools()``; a lean registry whose ``tools`` are already
    arcrun tools has no such method, so fall back to the values directly.
    """
    to_arcrun = getattr(registry, "to_arcrun_tools", None)
    if callable(to_arcrun):
        return list(to_arcrun())
    return list(registry.tools.values())


__all__ = ["BuiltDoor", "build_door_from_agent", "build_door_from_started_agent"]
