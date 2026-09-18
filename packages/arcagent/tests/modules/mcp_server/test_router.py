"""SPEC-082 T-1110 (RED) — the shared, transport-agnostic ``DoorRouter``.

Phase 5 makes the door serve. Today ``HttpDoor`` holds the JSON-RPC logic
(``_handle`` / ``_list`` / ``_call``) inline, so a second transport (stdio) would
have to duplicate it. T-1110 extracts that logic into
``arcagent.modules.mcp_server.router.DoorRouter`` — a transport-agnostic handler
with one async method ``handle(message: dict) -> dict`` that returns the JSON-RPC
response envelope. ``HttpDoor`` will then delegate to it (behaviour unchanged).

This test defines the contract ``HttpDoor`` must delegate to: for the SAME inputs,
``DoorRouter.handle`` produces the SAME JSON-RPC response envelope ``HttpDoor``
produces today — for ``server/discover``, allowlist-filtered ``tools/list``, an
allowlisted ``tools/call``, and a denied ``tools/call`` (JSON-RPC error).

RED: the ``router`` module does not exist yet. The import
``from arcagent.modules.mcp_server.router import DoorRouter`` fails with
``No module named 'arcagent.modules.mcp_server.router'``. It goes GREEN when T-1110
adds the module and ``HttpDoor`` delegates to it.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from arcrun import Tool
from arcteam.crypto import ReplayCache, new_nonce
from arctrust import AuditEvent, generate_keypair
from arctrust import identity as arc_identity

from arcagent.capabilities.provider import AgentCapabilityProvider
from arcagent.modules.mcp_server.allowlist import ExposureAllowlist
from arcagent.modules.mcp_server.config import McpServerConfig
from arcagent.modules.mcp_server.http_transport import HttpDoor
from arcagent.modules.mcp_server.identity import sign_inbound
from arcagent.modules.mcp_server.router import DoorRouter  # RED: module absent today
from arcagent.modules.mcp_server.server import McpServer

_ACCESS_DENIED = -32001


def _now() -> str:
    return datetime.now(UTC).isoformat()


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"the {name} tool"
        self.input_schema: dict[str, Any] = {"type": "object", "properties": {}}


class _FakeRegistry:
    def __init__(self, tools: dict[str, _FakeTool]) -> None:
        self.tools = tools


def _server() -> McpServer:
    registry = _FakeRegistry({"read_file": _FakeTool("read_file"), "list_dir": _FakeTool("list_dir")})
    return McpServer(registry)  # type: ignore[arg-type]


def _provider(did: str) -> AgentCapabilityProvider:
    async def _execute(args: dict[str, Any], ctx: Any) -> str:
        return f"ran read_file({args})"

    return AgentCapabilityProvider(
        tools=[
            Tool(
                name="read_file",
                description="read a file",
                input_schema={"type": "object", "properties": {}},
                execute=_execute,
            )
        ],
        skills=[],
        tier="personal",
        caller_did=did,
    )


def _allowlist() -> ExposureAllowlist:
    # Expose only read_file; list_dir is filtered out of tools/list and
    # delete_file is refused pre-dispatch on tools/call.
    return ExposureAllowlist.from_config(
        McpServerConfig(enabled=True, expose=["read_file"]), tier="personal"
    )


def _call_message(verb: str, did: str, kp: Any, msg_id: int) -> dict[str, Any]:
    """A signed ``tools/call`` JSON-RPC message carrying its envelope in ``_meta``."""
    content = {"method": "tools/call", "params": {"name": verb, "arguments": {"path": "/x"}}}
    request = sign_inbound(
        content,
        nonce=new_nonce(),
        ts=_now(),
        caller_did=did,
        private_key=kp.private_key,
        public_key=kp.public_key,
    )
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "method": "tools/call",
        "params": {
            "name": verb,
            "arguments": {"path": "/x"},
            "_meta": {
                "arc/callerDid": did,
                "arc/publicKey": base64.b64encode(kp.public_key).decode(),
                "arc/signature": base64.b64encode(request.signature).decode(),
                "arc/nonce": request.nonce,
                "arc/ts": request.ts,
            },
        },
    }


async def _http_response(door: HttpDoor, message: dict[str, Any]) -> dict[str, Any]:
    """Drive an ``HttpDoor`` once over ASGI and return the parsed JSON-RPC envelope."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=door), base_url="http://door"
    ) as client:
        response = await client.post("/", json=message)
    return response.json()


@pytest.mark.asyncio
async def test_discover_matches_http_door() -> None:
    """``DoorRouter.handle`` returns the exact ``server/discover`` envelope HttpDoor does."""
    message = {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}}

    router = DoorRouter(_server(), tier="personal")
    http_door = HttpDoor(_server(), tier="personal")

    router_resp = await router.handle(message)
    http_resp = await _http_response(http_door, message)

    assert router_resp == http_resp


@pytest.mark.asyncio
async def test_tools_list_is_allowlist_filtered_and_matches_http_door() -> None:
    """``tools/list`` returns only the allowlisted verb, identically to HttpDoor."""
    message = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}

    router = DoorRouter(_server(), tier="personal", allowlist=_allowlist())
    http_door = HttpDoor(_server(), tier="personal", allowlist=_allowlist())

    router_resp = await router.handle(message)
    http_resp = await _http_response(http_door, message)

    names = {tool["name"] for tool in router_resp["result"]["tools"]}
    assert names == {"read_file"}
    assert router_resp == http_resp


@pytest.mark.asyncio
async def test_allowlisted_call_matches_http_door() -> None:
    """A signed, allowlisted ``tools/call`` returns the same result body as HttpDoor."""
    kp = generate_keypair()
    did = arc_identity.did_from_public_key(kp.public_key, org="acme", agent_type="exec")
    message = _call_message("read_file", did, kp, msg_id=7)

    router = DoorRouter(
        _server(),
        tier="personal",
        provider=_provider(did),
        allowlist=_allowlist(),
        replay_cache=ReplayCache(),
        audit_sink=_RecordingSink(),
    )
    http_door = HttpDoor(
        _server(),
        tier="personal",
        provider=_provider(did),
        allowlist=_allowlist(),
        replay_cache=ReplayCache(),
        audit_sink=_RecordingSink(),
    )

    router_resp = await router.handle(message)
    http_resp = await _http_response(http_door, message)

    assert router_resp["result"]["isError"] is False
    assert "ran read_file" in router_resp["result"]["content"][0]["text"]
    assert router_resp == http_resp


@pytest.mark.asyncio
async def test_denied_call_returns_jsonrpc_error_matching_http_door() -> None:
    """A signed but non-allowlisted verb yields a JSON-RPC error, same as HttpDoor."""
    kp = generate_keypair()
    did = arc_identity.did_from_public_key(kp.public_key, org="acme", agent_type="exec")
    message = _call_message("delete_file", did, kp, msg_id=9)

    router = DoorRouter(
        _server(),
        tier="personal",
        provider=_provider(did),
        allowlist=_allowlist(),
        replay_cache=ReplayCache(),
        audit_sink=_RecordingSink(),
    )
    http_door = HttpDoor(
        _server(),
        tier="personal",
        provider=_provider(did),
        allowlist=_allowlist(),
        replay_cache=ReplayCache(),
        audit_sink=_RecordingSink(),
    )

    router_resp = await router.handle(message)
    http_resp = await _http_response(http_door, message)

    assert "result" not in router_resp
    assert router_resp["error"]["code"] == _ACCESS_DENIED
    assert router_resp["error"] == http_resp["error"]
