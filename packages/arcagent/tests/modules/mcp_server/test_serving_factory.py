"""SPEC-082 T-1112 / SPEC-084 T-1146 — the build-from-agent factory.

arcagent stays headless (it never binds a port). A CLI (or the arcui mount) serves
the door, but first it must ASSEMBLE the door from a started agent.
``arcagent.modules.mcp_server.serving.build_door_from_agent(agent)`` is the factory
that wires ``McpServer(registry)`` + ``ExposureAllowlist.from_config(config, tier)``
+ ``AgentCapabilityProvider`` + ``ReplayCache`` + a tiered audit sink into the door's
``mcp`` SDK server (``BuiltDoor.server``) and its ``HttpDoor`` ASGI app, reading only
the agent's public surface.

Two behaviours are the contract:

1. The built door serves the agent's exposed tool through a REAL SDK ``list_tools``.
2. The factory REFUSES (raises ``ValueError``) when ``[modules.mcp_server]`` is
   disabled — a door must never serve when the operator did not enable it
   (default-off, security-sensitive surface).

The fake agent below exposes only what the factory reads. Test #1 succeeding proves
that surface is complete, so test #2 flipping a single field (``enabled=False``)
isolates the refusal — a raise there is the disabled-door contract, not a missing
attribute.
"""

from __future__ import annotations

from typing import Any

import pytest
from arcrun import Tool
from arctrust import AuditEvent
from arctrust import identity as arc_identity
from mcp.shared.memory import create_connected_server_and_client_session

from arcagent.modules.mcp_server.config import McpServerConfig
from arcagent.modules.mcp_server.serving import build_door_from_agent


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


@pytest.mark.asyncio
async def test_built_door_lists_the_exposed_tool() -> None:
    """The door the factory builds serves the agent's exposed tool over a real SDK client."""
    built = build_door_from_agent(_FakeAgent(enabled=True))

    async with create_connected_server_and_client_session(built.server) as session:
        listed = await session.list_tools()

    names = {tool.name for tool in listed.tools}
    assert names == {"read_file"}


def test_factory_refuses_when_mcp_server_disabled() -> None:
    """A disabled ``[modules.mcp_server]`` must refuse to build a serving door."""
    with pytest.raises(ValueError):
        build_door_from_agent(_FakeAgent(enabled=False))
