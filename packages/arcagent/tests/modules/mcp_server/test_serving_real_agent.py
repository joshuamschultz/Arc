"""SPEC-082 /review backfill — the REAL serving path, end to end.

The factory tests in ``test_serving_factory.py`` prove the wiring against a
hand-built fake agent. The review found the *real* serving path
(``build_door_from_started_agent`` over a genuinely started :class:`ArcAgent`) had
only ever been exercised through mocks, and that the federal enrollment roster —
just wired ``config.enrolled → DoorRouter → authorize_and_dispatch`` — had no
end-to-end proof that it is consulted rather than defaulting to ``None``.

This file closes both gaps with committed, non-trivial assertions:

1. A genuinely started agent, door enabled, serves its *real* tool catalog through
   the built router's ``tools/list`` (non-empty; every descriptor names a tool and
   carries an ``inputSchema``).
2. ``build_door_from_started_agent`` refuses (``ValueError``) a started agent whose
   ``[modules.mcp_server]`` is absent/disabled.
3. FEDERAL ROSTER: a signed ``tools/call`` from an ENROLLED caller DID is admitted
   (dispatched to a real result) while a signed call from an UNENROLLED but valid
   DID is refused with JSON-RPC ``-32001`` — driven through the door the *factory*
   assembles, so the roster is proven wired through serving → router → dispatch.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from arcrun import Tool
from arcteam.crypto import new_nonce
from arctrust import AuditEvent, generate_keypair
from arctrust import identity as arc_identity

import arcagent
from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    ContextConfig,
    IdentityConfig,
    LLMConfig,
    ModuleEntry,
    TelemetryConfig,
)
from arcagent.modules.mcp_server.config import McpServerConfig
from arcagent.modules.mcp_server.identity import sign_inbound
from arcagent.modules.mcp_server.serving import build_door_from_agent
from arcagent.modules.mcp_server.serving import (
    build_door_from_started_agent as _build_from_started,
)

_TOOL = "read_file"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _agent_config(tmp_path: Path, *, modules: dict[str, ModuleEntry]) -> ArcAgentConfig:
    """A minimal but real :class:`ArcAgentConfig` a fresh agent can start from."""
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    return ArcAgentConfig(
        agent=AgentConfig(
            name="probe", org="testorg", type="executor", workspace=str(workspace)
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(did="", key_dir=str(tmp_path / "keys"), vault_path=""),
        telemetry=TelemetryConfig(enabled=False),
        context=ContextConfig(max_tokens=10000),
        modules=modules,
    )


@pytest.mark.asyncio
async def test_started_agent_serves_its_real_tool_catalog(tmp_path: Path) -> None:
    """A genuinely started agent serves its real tools through the built door."""
    config = _agent_config(
        tmp_path,
        modules={"mcp_server": ModuleEntry(enabled=True, config={"expose": ["*"]})},
    )
    agent = ArcAgent(config=config)
    await agent.startup()
    try:
        built = arcagent.build_mcp_door(agent)
        response = await built.router.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
    finally:
        await agent.shutdown()

    tools = response["result"]["tools"]
    # The agent ships real builtins — the catalog must be non-empty, not a stub.
    assert tools, "the started agent served an empty tool catalog"
    for tool in tools:
        assert tool["name"], "a served tool descriptor has no name"
        assert "inputSchema" in tool, f"{tool['name']} served without an inputSchema"


@pytest.mark.asyncio
async def test_started_agent_refuses_a_disabled_door(tmp_path: Path) -> None:
    """With no ``[modules.mcp_server]`` entry the real serving factory refuses to build."""
    config = _agent_config(tmp_path, modules={})
    agent = ArcAgent(config=config)
    await agent.startup()
    try:
        with pytest.raises(ValueError, match="disabled"):
            _build_from_started(agent)
    finally:
        await agent.shutdown()


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _FakeRegistry:
    def __init__(self, tools: dict[str, Tool]) -> None:
        self.tools = tools


class _FakeFederalAgent:
    """A started-agent surface at federal tier with a named exposure + enrolled roster.

    A real federal agent cannot carry ``expose=['*']`` (the allowlist refuses an
    unbounded exposure at federal), so the roster proof uses the same public surface
    ``build_door_from_agent`` reads, with an explicit exposure and a one-DID roster.
    """

    def __init__(self, *, enrolled_did: str) -> None:
        async def _execute(args: dict[str, Any], ctx: Any) -> str:
            return f"ran {_TOOL}({args})"

        self.tool_registry = _FakeRegistry(
            {
                _TOOL: Tool(
                    name=_TOOL,
                    description="read a file",
                    input_schema={"type": "object", "properties": {}},
                    execute=_execute,
                )
            }
        )
        self.did = arc_identity.did_from_public_key(
            b"\x11" * 32, org="acme", agent_type="exec"
        )
        self.tier = "federal"
        self.mcp_config = McpServerConfig(
            enabled=True, expose=[_TOOL], enrolled=[enrolled_did]
        )
        self.audit_sink = _RecordingSink()


def _signed_call_message(did: str, keypair: Any, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """A JSON-RPC ``tools/call`` carrying a real signed envelope in ``params._meta``.

    The signed ``content`` mirrors exactly what the router reconstructs from the
    message, so the signature verifies against the door's canonical bytes.
    """
    arguments = args or {}
    content = {"method": "tools/call", "params": {"name": _TOOL, "arguments": arguments}}
    request = sign_inbound(
        content,
        nonce=new_nonce(),
        ts=_now(),
        caller_did=did,
        private_key=keypair.private_key,
        public_key=keypair.public_key,
    )
    return {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "tools/call",
        "params": {
            "name": _TOOL,
            "arguments": arguments,
            "_meta": {
                "arc/callerDid": did,
                "arc/publicKey": base64.b64encode(keypair.public_key).decode(),
                "arc/signature": base64.b64encode(request.signature).decode(),
                "arc/nonce": request.nonce,
                "arc/ts": request.ts,
            },
        },
    }


@pytest.mark.asyncio
async def test_federal_roster_admits_the_enrolled_caller() -> None:
    """A signed tools/call from an ENROLLED DID is dispatched through the built door."""
    enrolled_kp = generate_keypair()
    enrolled_did = arc_identity.did_from_public_key(
        enrolled_kp.public_key, org="acme", agent_type="exec"
    )
    built = build_door_from_agent(_FakeFederalAgent(enrolled_did=enrolled_did))

    response = await built.router.handle(
        _signed_call_message(enrolled_did, enrolled_kp, {"path": "/x"})
    )

    assert "error" not in response, f"enrolled caller was refused: {response.get('error')}"
    result = response["result"]
    assert result["isError"] is False
    assert result["content"][0]["text"] == "ran read_file({'path': '/x'})"


@pytest.mark.asyncio
async def test_federal_roster_refuses_an_unenrolled_valid_caller() -> None:
    """A signed tools/call from a valid but UNENROLLED DID is refused ``-32001``.

    This is the roster proof: same door, same real signature, only the DID differs
    from the enrolled one — so an admit here would mean ``enrolled`` defaulted to
    ``None`` instead of being wired through the factory.
    """
    enrolled_kp = generate_keypair()
    enrolled_did = arc_identity.did_from_public_key(
        enrolled_kp.public_key, org="acme", agent_type="exec"
    )
    built = build_door_from_agent(_FakeFederalAgent(enrolled_did=enrolled_did))

    other_kp = generate_keypair()
    other_did = arc_identity.did_from_public_key(
        other_kp.public_key, org="acme", agent_type="exec"
    )
    response = await built.router.handle(_signed_call_message(other_did, other_kp))

    assert "result" not in response
    assert response["error"]["code"] == -32001
    assert "not enrolled" in response["error"]["message"]
