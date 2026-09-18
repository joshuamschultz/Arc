"""SPEC-082 — gaps the door's other tests leave open (test-author flagged).

The Phase-2 tests prove each door piece and prove the federal door *refuses* a
certless request. They do not prove the door ever *serves* a request at federal
(so a door that refused everything would still pass), nor that the audit payload
hash is stable across argument key order, nor that the composed pipeline
(verify → allowlist → dispatch → audit) actually runs end to end. These do.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any

import pytest
from arcrun import Tool
from arcteam.crypto import ReplayCache, new_nonce
from arctrust import AuditEvent, generate_keypair
from arctrust import identity as arc_identity

from arcagent.capabilities.provider import AgentCapabilityProvider
from arcagent.modules.mcp_server.allowlist import ExposureAllowlist
from arcagent.modules.mcp_server.audit import emit_door_event
from arcagent.modules.mcp_server.config import McpServerConfig
from arcagent.modules.mcp_server.door import authorize_and_dispatch
from arcagent.modules.mcp_server.http_transport import HttpDoor
from arcagent.modules.mcp_server.identity import sign_inbound
from arcagent.modules.mcp_server.server import McpServer


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
    registry = _FakeRegistry({"read_file": _FakeTool("read_file")})
    return McpServer(registry)  # type: ignore[arg-type]


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _drive(door: HttpDoor, scope: dict[str, Any], body: bytes) -> tuple[int, dict[str, Any]]:
    """Invoke an ASGI app once with a full body, returning (status, json)."""
    sent: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive() -> dict[str, Any]:
        return events.pop(0)

    async def send(event: dict[str, Any]) -> None:
        sent.append(event)

    await door(scope, receive, send)
    status = next(e["status"] for e in sent if e["type"] == "http.response.start")
    raw = b"".join(e.get("body", b"") for e in sent if e["type"] == "http.response.body")
    import json

    return status, json.loads(raw)


def _http_scope(*, body: bytes, tls: dict[str, Any] | None = None) -> dict[str, Any]:
    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(b"content-length", str(len(body)).encode())],
    }
    if tls is not None:
        scope["extensions"] = {"tls": tls}
    return scope


def test_canonical_payload_hash_is_stable_across_argument_key_order() -> None:
    """Two arg dicts with the same content in different key order hash identically."""
    ordered = _RecordingSink()
    shuffled = _RecordingSink()

    emit_door_event(
        ordered,
        caller_did="did:arc:acme:exec/deadbeef",
        verb="tools/call",
        outcome="allow",
        tier="personal",
        arguments={"a": 1, "b": 2, "c": 3},
    )
    emit_door_event(
        shuffled,
        caller_did="did:arc:acme:exec/deadbeef",
        verb="tools/call",
        outcome="allow",
        tier="personal",
        arguments={"c": 3, "b": 2, "a": 1},
    )

    assert ordered.events[0].payload_hash == shuffled.events[0].payload_hash


@pytest.mark.asyncio
async def test_federal_door_serves_a_request_that_presents_a_client_certificate() -> None:
    """The federal mTLS rule refuses the certless and *serves* the certful (REQ-418)."""
    import json

    door = HttpDoor(_server(), tier="federal")
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    body = json.dumps(message).encode()
    scope = _http_scope(body=body, tls={"client_cert_chain": ["-----BEGIN CERTIFICATE-----"]})

    status, payload = await _drive(door, scope, body)

    assert status == 200
    assert {tool["name"] for tool in payload["result"]["tools"]} == {"read_file"}


@pytest.mark.asyncio
async def test_composed_pipeline_verifies_allowlists_dispatches_and_audits() -> None:
    """A signed, allowlisted call runs the whole pipeline and emits one allow event."""
    keypair = generate_keypair()
    did = arc_identity.did_from_public_key(keypair.public_key, org="acme", agent_type="exec")

    async def _execute(args: dict[str, Any], ctx: Any) -> str:
        return f"ran search({args})"

    provider = AgentCapabilityProvider(
        tools=[
            Tool(
                name="search",
                description="search",
                input_schema={"type": "object", "properties": {}},
                execute=_execute,
            )
        ],
        skills=[],
        tier="personal",
        caller_did=did,
    )
    allowlist = ExposureAllowlist.from_config(
        McpServerConfig(enabled=True, expose=["search"]), tier="personal"
    )
    sink = _RecordingSink()
    content = {"method": "tools/call", "params": {"name": "search", "arguments": {"x": "hi"}}}
    request = sign_inbound(
        content,
        nonce=new_nonce(),
        ts=_now(),
        caller_did=did,
        private_key=keypair.private_key,
        public_key=keypair.public_key,
    )

    result = await authorize_and_dispatch(
        request,
        provider=provider,
        allowlist=allowlist,
        replay_cache=ReplayCache(),
        audit_sink=sink,
        tier="personal",
    )

    assert "ran search" in result.content
    assert result.is_error is False
    allows = [e for e in sink.events if e.outcome == "allow"]
    assert len(allows) == 1
    assert allows[0].action == "mcp.tools/call"
    assert allows[0].actor_did == did


@pytest.mark.asyncio
async def test_composed_call_travels_the_http_wire_end_to_end() -> None:
    """A signed tools/call over the HTTP responder reaches the tool and returns its text."""
    import json

    keypair = generate_keypair()
    did = arc_identity.did_from_public_key(keypair.public_key, org="acme", agent_type="exec")

    async def _execute(args: dict[str, Any], ctx: Any) -> str:
        return f"ran read_file({args})"

    provider = AgentCapabilityProvider(
        tools=[
            Tool(
                name="read_file",
                description="read",
                input_schema={"type": "object", "properties": {}},
                execute=_execute,
            )
        ],
        skills=[],
        tier="personal",
        caller_did=did,
    )
    allowlist = ExposureAllowlist.from_config(
        McpServerConfig(enabled=True, expose=["read_file"]), tier="personal"
    )
    door = HttpDoor(
        _server(),
        tier="personal",
        provider=provider,
        allowlist=allowlist,
        replay_cache=ReplayCache(),
        audit_sink=_RecordingSink(),
    )

    content = {"method": "tools/call", "params": {"name": "read_file", "arguments": {"path": "/x"}}}
    request = sign_inbound(
        content,
        nonce=new_nonce(),
        ts=_now(),
        caller_did=did,
        private_key=keypair.private_key,
        public_key=keypair.public_key,
    )
    message = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {
            "name": "read_file",
            "arguments": {"path": "/x"},
            "_meta": {
                "arc/callerDid": did,
                "arc/publicKey": base64.b64encode(keypair.public_key).decode(),
                "arc/signature": base64.b64encode(request.signature).decode(),
                "arc/nonce": request.nonce,
                "arc/ts": request.ts,
            },
        },
    }
    body = json.dumps(message).encode()

    status, payload = await _drive(door, _http_scope(body=body), body)

    assert status == 200
    assert payload["result"]["isError"] is False
    assert "ran read_file" in payload["result"]["content"][0]["text"]
