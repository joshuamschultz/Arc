"""SPEC-082 T-1112 (RED) — the build-from-agent factory.

arcagent stays headless (it never binds a port). A CLI surface serves the door, but
first it must ASSEMBLE the door from a started agent. T-1112 adds
``arcagent.modules.mcp_server.serving.build_door_from_agent(agent)`` — the factory
that wires ``McpServer(registry)`` + ``ExposureAllowlist.from_config(config, tier)``
+ ``AgentCapabilityProvider`` + ``ReplayCache`` + a tiered audit sink into a
``DoorRouter`` (and its ``HttpDoor`` ASGI app), reading only the agent's public
surface.

Two behaviours are the contract:

1. The built router answers ``tools/list`` with the agent's exposed tool.
2. The factory REFUSES (raises ``ValueError``) when ``[modules.mcp_server]`` is
   disabled — a door must never serve when the operator did not enable it
   (default-off, security-sensitive surface).

The fake agent below exposes only what the factory reads. Test #1 succeeding proves
that surface is complete, so test #2 flipping a single field (``enabled=False``)
isolates the refusal — a raise there is the disabled-door contract, not a missing
attribute.

RED: the ``serving`` module does not exist yet. The import
``from arcagent.modules.mcp_server.serving import build_door_from_agent`` fails with
``No module named 'arcagent.modules.mcp_server.serving'``. It goes GREEN when T-1112
adds it.
"""

from __future__ import annotations

from typing import Any

import pytest
from arcrun import Tool
from arctrust import AuditEvent
from arctrust import identity as arc_identity

from arcagent.modules.mcp_server.config import McpServerConfig
from arcagent.modules.mcp_server.serving import (  # RED: module absent today
    build_door_from_agent,
)


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _FakeRegistry:
    """A tool registry exposing the ``tools`` mapping the factory reads."""

    def __init__(self, tools: dict[str, Tool]) -> None:
        self.tools = tools


class _FakeAgent:
    """A minimal started agent exposing only the surface the factory consumes.

    ``tool_registry`` (with ``.tools``), ``did``, ``tier``, ``mcp_config``
    (``McpServerConfig``), and ``audit_sink`` — the collaborators
    ``build_door_from_agent`` needs to assemble the door.
    """

    def __init__(self, *, enabled: bool) -> None:
        async def _execute(args: dict[str, Any], ctx: Any) -> str:
            return f"ran read_file({args})"

        self.tool_registry = _FakeRegistry(
            {
                "read_file": Tool(
                    name="read_file",
                    description="read a file",
                    input_schema={"type": "object", "properties": {}},
                    execute=_execute,
                )
            }
        )
        self.did = arc_identity.did_from_public_key(b"\x11" * 32, org="acme", agent_type="exec")
        self.tier = "personal"
        self.mcp_config = McpServerConfig(enabled=enabled, expose=["read_file"])
        self.audit_sink = _RecordingSink()


def _router_of(built: Any) -> Any:
    """The DoorRouter, whether the factory returns it directly or wraps it."""
    return getattr(built, "router", built)


@pytest.mark.asyncio
async def test_built_router_lists_the_exposed_tool() -> None:
    """The door the factory builds answers tools/list with the agent's exposed tool."""
    built = build_door_from_agent(_FakeAgent(enabled=True))

    response = await _router_of(built).handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )

    names = {tool["name"] for tool in response["result"]["tools"]}
    assert names == {"read_file"}


def test_factory_refuses_when_mcp_server_disabled() -> None:
    """A disabled ``[modules.mcp_server]`` must refuse to build a serving door."""
    with pytest.raises(ValueError):
        build_door_from_agent(_FakeAgent(enabled=False))
