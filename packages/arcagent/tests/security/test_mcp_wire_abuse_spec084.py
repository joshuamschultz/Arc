"""SPEC-084 T-1153 abuse battery — the MCP *wire* refuses every hostile inbound.

Registered in the cross-package adversarial battery
(``tests/run_adversarial_tests.py``). These are *abuse cases*, not TDD unit tests:
each drives the SHIPPED door across the **real ``mcp`` SDK wire** — a genuine
``ClientSession`` over ``httpx.ASGITransport`` for envelope-carrying calls, and a
raw HTTP POST for transport-frame attacks — and asserts the door **fails closed**
without a crash and without an un-audited pass-through.

They are the wire-level complement to ``test_mcp_door_abuse_spec082.py`` (which
drives the ``verify_inbound`` / ``authorize_and_dispatch`` pipeline *functions*
directly) and to ``test_door_sdk_client.py`` (which proves the happy path plus the
mTLS-certless, oversized-body, and unenrolled-federal wire guards). This file adds
the wire attacks none of those cover: a malformed / non-MCP body, an unsupported
protocol version, an unsigned ``tools/call`` over the real client, a replayed
envelope, and a ``_meta`` signed over different content than the call.

As an abuse battery these guards PASS on commit — the envelope already refuses
each attack; that is the point. A positive control (``test_signed_wire_call_is_
served``) proves the door still serves a legitimate call, so a door that refused
*everything*, or one whose deny path stopped auditing, fails this suite rather than
passing vacuously.

Threat coverage (README "Threat surface touched"):
- LLM05 improper input handling — a non-MCP body is refused, never dispatched;
- LLM10 unbounded consumption / graceful negotiation — a bogus protocol version is
  negotiated down, not crashed on;
- ASI07 forged/replayed control action — unsigned envelope, replayed nonce;
- ASI02/ASI03 tool/identity abuse — a signature over different content than the call.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from arcrun import Tool
from arcteam.crypto import ReplayCache, new_nonce
from arctrust import AuditEvent, generate_keypair
from arctrust import identity as arc_identity
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.exceptions import McpError

from arcagent.capabilities.provider import AgentCapabilityProvider
from arcagent.modules.mcp_server.allowlist import ExposureAllowlist
from arcagent.modules.mcp_server.config import McpServerConfig
from arcagent.modules.mcp_server.http_transport import HttpDoor
from arcagent.modules.mcp_server.identity import InboundRequest, sign_inbound
from arcagent.modules.mcp_server.server import McpServer

_ORG = "acme"
_TYPE = "exec"
_TOOL = "echo"
#: A short client timeout keeps a non-conformant door from hanging the test.
_CLIENT_TIMEOUT_S = 5.0
#: The MCP streamable-HTTP transport requires the client to accept both media types.
_WIRE_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class _RecordingSink:
    """An ``arctrust`` ``AuditSink`` that keeps every event for assertions."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def denials(self) -> list[AuditEvent]:
        return [event for event in self.events if event.outcome == "deny"]

    def allows(self) -> list[AuditEvent]:
        return [event for event in self.events if event.outcome == "allow"]


class _FakeTool:
    """The three fields ``McpServer`` reads off a registry entry."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"the {name} tool"
        self.input_schema: dict[str, Any] = {"type": "object", "properties": {}}


class _FakeRegistry:
    def __init__(self, tools: dict[str, _FakeTool]) -> None:
        self.tools = tools


def _member_did(public_key: bytes) -> str:
    return arc_identity.did_from_public_key(public_key, org=_ORG, agent_type=_TYPE)


def _provider(did: str) -> AgentCapabilityProvider:
    async def _execute(args: dict[str, Any], ctx: Any) -> str:
        return f"ran {_TOOL}({args})"

    return AgentCapabilityProvider(
        tools=[
            Tool(
                name=_TOOL,
                description="echo a value",
                input_schema={"type": "object", "properties": {}},
                execute=_execute,
            )
        ],
        skills=[],
        tier="personal",
        caller_did=did,
    )


def _build_door(sink: _RecordingSink, agent_did: str) -> HttpDoor:
    """A full ``tools/call``-enabled personal-tier door over the ``echo`` tool."""
    return HttpDoor(
        McpServer(_FakeRegistry({_TOOL: _FakeTool(_TOOL)})),  # type: ignore[arg-type]
        tier="personal",
        provider=_provider(agent_did),
        allowlist=ExposureAllowlist.from_config(
            McpServerConfig(enabled=True, expose=[_TOOL]), tier="personal"
        ),
        replay_cache=ReplayCache(),
        audit_sink=sink,
    )


def _signed_request(did: str, kp: Any, args: dict[str, Any]) -> InboundRequest:
    """Sign the exact ``tools/call`` content the door reconstructs from the wire."""
    content = {"method": "tools/call", "params": {"name": _TOOL, "arguments": args}}
    return sign_inbound(
        content,
        nonce=new_nonce(),
        ts=_now(),
        caller_did=did,
        private_key=kp.private_key,
        public_key=kp.public_key,
    )


def _arc_meta(request: InboundRequest) -> dict[str, Any]:
    """The Arc identity envelope as SDK ``_meta`` keys (bytes fields base64-encoded)."""
    return {
        "arc/callerDid": request.caller_did,
        "arc/publicKey": base64.b64encode(request.public_key).decode(),
        "arc/signature": base64.b64encode(request.signature).decode(),
        "arc/nonce": request.nonce,
        "arc/ts": request.ts,
    }


@asynccontextmanager
async def _sdk_session(door: HttpDoor) -> AsyncIterator[ClientSession]:
    """Open a REAL, initialized ``ClientSession`` over the door via ASGITransport."""

    def factory(
        headers: dict[str, str] | None = None,
        timeout: Any = None,
        auth: Any = None,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=door),
            base_url="http://door",
            headers=headers,
            timeout=timeout,
        )

    async with streamablehttp_client(
        "http://door/mcp", timeout=_CLIENT_TIMEOUT_S, httpx_client_factory=factory
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def _raw_post(door: HttpDoor, body: bytes, headers: dict[str, str]) -> httpx.Response:
    """POST a raw body straight at the door — the transport frame, no SDK client."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=door),
        base_url="http://door",
    ) as client:
        return await client.post("/mcp", content=body, headers=headers)


async def _refused(session: ClientSession, args: dict[str, Any], meta: dict[str, Any] | None) -> bool:
    """Whether a ``tools/call`` was refused — a raised ``McpError`` or an ``isError``."""
    try:
        result = await session.call_tool(_TOOL, args, meta=meta)
    except McpError:
        return True
    return bool(result.isError)


# --------------------------------------------------------------------------- #
# Case 1 — Malformed / non-MCP body (LLM05): garbage that is not JSON-RPC.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_malformed_non_mcp_body_is_refused_cleanly() -> None:
    """A body that is not JSON-RPC is refused at the transport with a parse error.

    The door must answer with an HTTP error and a well-formed JSON-RPC error
    envelope — no crash, and nothing reaches dispatch, so no ``allow`` is audited.
    """
    sink = _RecordingSink()
    door = _build_door(sink, _member_did(generate_keypair().public_key))

    response = await _raw_post(door, b"this is not json-rpc {{{", _WIRE_HEADERS)

    assert response.status_code == 400
    assert response.json()["error"], "the refusal must be a JSON-RPC error envelope"
    assert not sink.allows(), "a malformed body must never reach dispatch"


# --------------------------------------------------------------------------- #
# Case 2 — Unsupported protocol version (LLM10): a bogus initialize version.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_unsupported_protocol_version_is_negotiated_not_crashed() -> None:
    """An ``initialize`` carrying an unsupported protocol version is handled
    gracefully: the SDK server negotiates its OWN supported version rather than
    echoing the client's or falling over, and nothing is dispatched or audited.
    """
    bogus = "1900-01-01"
    sink = _RecordingSink()
    door = _build_door(sink, _member_did(generate_keypair().public_key))
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": bogus,
            "capabilities": {},
            "clientInfo": {"name": "hostile", "version": "1"},
        },
    }

    response = await _raw_post(door, json.dumps(init).encode(), _WIRE_HEADERS)

    assert response.status_code == 200, "the door must not fall over on a bad version"
    negotiated = response.json()["result"]["protocolVersion"]
    assert negotiated != bogus, "the server must negotiate its own version, not the client's"
    assert isinstance(negotiated, str) and negotiated, "a real version must be returned"
    assert not sink.allows(), "a handshake must never emit a tool-call allow"


# --------------------------------------------------------------------------- #
# Case 3 — Unsigned tools/call over the real SDK (ASI07): no ``_meta`` envelope.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_unsigned_wire_call_is_refused_and_audited() -> None:
    """A ``tools/call`` with no Arc envelope in ``_meta`` reconstructs to an empty
    caller DID, fails identity verification, and is refused with a ``deny`` audit —
    key possession, not a bare tool name, is what admits a caller.
    """
    sink = _RecordingSink()
    door = _build_door(sink, _member_did(generate_keypair().public_key))

    async with _sdk_session(door) as session:
        refused = await _refused(session, {"path": "/x"}, meta=None)

    assert refused, "an unsigned wire call must be refused"
    assert sink.denials(), "an unsigned wire call must emit a deny audit event"
    assert not sink.allows(), "an unsigned wire call must never reach dispatch"


# --------------------------------------------------------------------------- #
# Case 4 — Replayed envelope over the real SDK (ASI07): the same nonce twice.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_replayed_wire_envelope_is_refused_by_the_replay_cache() -> None:
    """The first presentation of a signed envelope is served; the SAME envelope
    replayed over the same door is refused by the door's ``ReplayCache`` and
    audited ``deny`` — exactly one dispatch is ever allowed.
    """
    kp = generate_keypair()
    did = _member_did(kp.public_key)
    sink = _RecordingSink()
    door = _build_door(sink, did)
    meta = _arc_meta(_signed_request(did, kp, {"path": "/x"}))

    async with _sdk_session(door) as session:
        first = await session.call_tool(_TOOL, {"path": "/x"}, meta=meta)
        replayed = await _refused(session, {"path": "/x"}, meta=meta)

    assert first.isError is False, "the first presentation must be served"
    assert replayed, "the replayed envelope must be refused"
    assert sink.denials(), "a replayed nonce must emit a deny audit event"
    assert len(sink.allows()) == 1, "the replay must not produce a second allow"


# --------------------------------------------------------------------------- #
# Case 5 — Tampered _meta over the real SDK (ASI02/ASI03): signature over other
# content than the call — the arguments are swapped after signing.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_tampered_meta_signature_over_other_content_fails_closed() -> None:
    """An envelope signed for ``{"path": "/x"}`` is presented on a call for
    ``{"path": "/evil"}``: the door reconstructs the call's own content, the
    signature no longer verifies, and the call is refused ``deny`` — a caller
    cannot sign a benign call and swap the arguments on the wire.
    """
    kp = generate_keypair()
    did = _member_did(kp.public_key)
    sink = _RecordingSink()
    door = _build_door(sink, did)
    meta = _arc_meta(_signed_request(did, kp, {"path": "/x"}))

    async with _sdk_session(door) as session:
        refused = await _refused(session, {"path": "/evil"}, meta=meta)

    assert refused, "a call whose arguments differ from the signed content must be refused"
    assert sink.denials(), "a tampered call must emit a deny audit event"
    assert not sink.allows(), "a tampered call must never reach dispatch"


# --------------------------------------------------------------------------- #
# Positive control — a legitimate signed wire call is served end to end, so none
# of the deny assertions above pass against a door that simply refuses all traffic.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_signed_wire_call_is_served() -> None:
    """A well-formed, key-bound, signed ``tools/call`` over the real SDK is served,
    with exactly one ``allow`` and no ``deny`` — proving the abuse cases above are
    the door discriminating hostile from legitimate, not blanket refusal.
    """
    kp = generate_keypair()
    did = _member_did(kp.public_key)
    sink = _RecordingSink()
    door = _build_door(sink, did)
    meta = _arc_meta(_signed_request(did, kp, {"path": "/x"}))

    async with _sdk_session(door) as session:
        result = await session.call_tool(_TOOL, {"path": "/x"}, meta=meta)

    assert result.isError is False
    assert any("ran" in block.text for block in result.content)  # type: ignore[union-attr]
    assert len(sink.allows()) == 1, "a legitimate call must be dispatched exactly once"
    assert not sink.denials(), "a legitimate call must not be denied"
