"""SPEC-084 T-1145 (RED) — a REAL ``mcp`` SDK client end to end through the door.

REQ-451/452 · COMP-002. T-1146 re-serves the Arc MCP door via the official ``mcp``
SDK server so a genuine :class:`mcp.ClientSession` — which does the Legacy
``initialize`` handshake and the streamable-HTTP transport — can connect, while the
SPEC-082 verify → enroll → allowlist → dispatch → audit pipeline
(:func:`~arcagent.modules.mcp_server.door.authorize_and_dispatch`) stays UNCHANGED:
the same policy decision and the same WORM audit record as the native path.

Today the door only answers the hand-rolled ``server/discover`` dialect
(:class:`~arcagent.modules.mcp_server.router.DoorRouter`), so a real
``ClientSession.initialize()`` cannot handshake with it. That is the feature-absent
RED anchor. Everything here drives the CURRENT
:class:`~arcagent.modules.mcp_server.http_transport.HttpDoor` ASGI app — the same
shell T-1146 keeps — through ``httpx.ASGITransport`` (no socket, no live server), so
the SDK client speaks to the in-process door exactly as an external client would.

RED map (verified against the current door):

- ``test_real_sdk_client_completes_handshake_and_lists_the_catalog`` — HEADLINE
  ANCHOR: a real ``ClientSession.initialize()`` then ``list_tools()`` must return
  the allowlisted catalog. Fails today because the door does not speak the real MCP
  ``initialize`` handshake — the SDK errors during connect. Goes GREEN at T-1146.
- ``test_call_tool_decision_and_audit_equal_the_native_path`` — a signed
  ``tools/call`` over the SAME real client yields the exact content, ``isError``,
  and ``allow`` audit event (action, outcome, actor DID, payload hash) that a direct
  ``authorize_and_dispatch`` of the identical signed request produces. The Arc
  identity envelope rides in the SDK ``_meta`` (the carrier proven by
  ``test_meta_carrier_round_trips_arc_envelope_keys`` below). Fails today at connect.
- ``test_federal_refuses_an_unenrolled_caller_and_audits_the_deny`` — at federal
  tier a validly-signed but UNENROLLED caller is refused (the SDK call raises or
  the result ``isError``) and the deny is audited — SPEC-082's enrollment gate,
  unchanged. Fails today at connect.
- ``test_meta_carrier_round_trips_arc_envelope_keys`` — GREEN CONTROL (passes
  today): proves the ``arc/*`` envelope placed in ``call_tool(meta=...)`` is
  retrievable server-side from ``request_context.meta`` against a scratch real SDK
  server, so T-1146 can read the envelope from ``_meta`` (not headers).
- ``test_federal_certless_request_is_still_refused`` /
  ``test_oversized_body_is_still_refused`` — GREEN GUARDS (pass today): the
  transport shell's mTLS and body-cap guards (REQ-452) survive T-1146's SDK
  integration; driven with raw ASGI scopes like ``test_http_transport.py``.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from arcrun import Tool
from arcteam.crypto import new_nonce
from arctrust import AuditEvent, ReplayCache, generate_keypair
from arctrust import identity as arc_identity
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.fastmcp import Context, FastMCP
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session

from arcagent.capabilities.provider import AgentCapabilityProvider
from arcagent.modules.mcp_server.allowlist import ExposureAllowlist
from arcagent.modules.mcp_server.config import McpServerConfig
from arcagent.modules.mcp_server.door import authorize_and_dispatch
from arcagent.modules.mcp_server.http_transport import HttpDoor
from arcagent.modules.mcp_server.identity import InboundRequest, sign_inbound
from arcagent.modules.mcp_server.server import McpServer

_ORG = "acme"
_TYPE = "exec"
_TOOL = "echo"
#: A short client timeout keeps a non-conformant door from hanging the test.
_CLIENT_TIMEOUT_S = 5.0
_EIGHT_MIB = 8 * 1024 * 1024


def _now() -> str:
    return datetime.now(UTC).isoformat()


class _RecordingSink:
    """An audit sink that captures every emitted event for assertion."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


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


def _provider(did: str, *, tier: str) -> AgentCapabilityProvider:
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
        tier=tier,
        caller_did=did,
    )


def _allowlist(tier: str) -> ExposureAllowlist:
    return ExposureAllowlist.from_config(McpServerConfig(enabled=True, expose=[_TOOL]), tier=tier)


def _build_door(
    *,
    tier: str,
    sink: _RecordingSink,
    agent_did: str,
    enrolled: frozenset[str] | None = None,
) -> HttpDoor:
    """A full ``tools/call``-enabled door over the ``echo`` tool for the given tier."""
    server = McpServer(_FakeRegistry({_TOOL: _FakeTool(_TOOL)}))  # type: ignore[arg-type]
    return HttpDoor(
        server,
        tier=tier,
        provider=_provider(agent_did, tier=tier),
        allowlist=_allowlist(tier),
        replay_cache=ReplayCache(),
        audit_sink=sink,
        enrolled=enrolled,
    )


def _signed_request(did: str, kp: Any, args: dict[str, Any]) -> InboundRequest:
    """Sign the exact ``tools/call`` content the door reconstructs from the wire.

    The signed content mirrors ``router._inbound_request`` /
    ``door._call_params``: ``{"method": "tools/call", "params": {"name", "arguments"}}``.
    T-1146's SDK handler must rebuild the same content from the tool name/arguments
    and the ``_meta`` envelope, so the signature verifies over an identical byte string.
    """
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


def _with_client_certificate(
    app: HttpDoor,
) -> Callable[[dict[str, Any], Any, Any], Awaitable[None]]:
    """Wrap the door ASGI app so every HTTP scope carries a TLS client-cert chain.

    ``httpx.ASGITransport`` builds a scope with no ``tls`` extension, so a hardened
    tier would 403 before the SDK could ever handshake. A real deployment terminates
    mTLS at a proxy that presents the client cert in the ASGI ``tls`` extension; this
    shim stands in for that proxy so a federal door is reachable over the SDK — the
    mTLS *rule* itself is exercised separately by ``test_federal_certless_...``.
    """

    async def wrapped(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            extensions = dict(scope.get("extensions") or {})
            extensions["tls"] = {"client_cert_chain": ["-----BEGIN CERTIFICATE-----"]}
            scope = {**scope, "extensions": extensions}
        await app(scope, receive, send)

    return wrapped


@asynccontextmanager
async def _sdk_session(
    door: HttpDoor, *, present_client_certificate: bool = False
) -> AsyncIterator[ClientSession]:
    """Open a REAL, initialized ``ClientSession`` over the door via ASGITransport.

    The SDK's ``streamablehttp_client`` drives the door in-process — no socket — by
    routing its httpx client through ``ASGITransport``. ``initialize()`` performs the
    genuine MCP handshake, which is exactly what today's door cannot answer.
    """
    app = _with_client_certificate(door) if present_client_certificate else door

    def factory(
        headers: dict[str, str] | None = None,
        timeout: Any = None,
        auth: Any = None,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
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


# --- RED anchors: the real SDK client against the door -------------------------


@pytest.mark.asyncio
async def test_real_sdk_client_completes_handshake_and_lists_the_catalog() -> None:
    """HEADLINE RED: a real SDK client handshakes and lists the allowlisted catalog.

    Fails today because the door speaks only ``server/discover`` — a genuine
    ``ClientSession.initialize()`` cannot negotiate with it, so the SDK errors during
    connect. Goes GREEN when T-1146 serves the door via the SDK server.
    """
    sink = _RecordingSink()
    agent_did = _member_did(generate_keypair().public_key)
    door = _build_door(tier="personal", sink=sink, agent_did=agent_did)

    async with _sdk_session(door) as session:
        listed = await session.list_tools()

    assert {tool.name for tool in listed.tools} == {_TOOL}


@pytest.mark.asyncio
async def test_call_tool_decision_and_audit_equal_the_native_path() -> None:
    """RED: a signed ``tools/call`` over the SDK equals the native pipeline exactly.

    The identical signed request is run two ways — through the SDK door (envelope in
    ``_meta``) and through ``authorize_and_dispatch`` directly — and the returned
    content, ``isError``, and the ``allow`` audit event must match. Separate replay
    caches (fresh per door / per native call) let the same nonce serve both paths.
    """
    kp = generate_keypair()
    did = _member_did(kp.public_key)
    args = {"path": "/x"}
    request = _signed_request(did, kp, args)

    native_sink = _RecordingSink()
    native = await authorize_and_dispatch(
        request,
        provider=_provider(did, tier="personal"),
        allowlist=_allowlist("personal"),
        replay_cache=ReplayCache(),
        audit_sink=native_sink,
        tier="personal",
    )

    door_sink = _RecordingSink()
    door = _build_door(tier="personal", sink=door_sink, agent_did=did)
    async with _sdk_session(door) as session:
        result = await session.call_tool(_TOOL, args, meta=_arc_meta(request))

    # Same tool output, surfaced through the SDK's TextContent envelope.
    assert [block.text for block in result.content] == [native.content]  # type: ignore[union-attr]
    assert bool(result.isError) == native.is_error

    # Same audit decision: one allow event, identical action / outcome / actor / hash.
    sdk_allow = _one_allow(door_sink)
    native_allow = _one_allow(native_sink)
    assert (sdk_allow.action, sdk_allow.outcome, sdk_allow.actor_did) == (
        native_allow.action,
        native_allow.outcome,
        native_allow.actor_did,
    )
    assert sdk_allow.actor_did == did
    assert sdk_allow.payload_hash == native_allow.payload_hash


@pytest.mark.asyncio
async def test_federal_refuses_an_unenrolled_caller_and_audits_the_deny() -> None:
    """RED: at federal a validly-signed but unenrolled caller is refused and audited.

    The enrollment gate is per-DID (SPEC-082): a different DID is enrolled, so this
    valid, signed caller is refused. The SDK surfaces the refusal (a raised
    ``McpError`` or an ``isError`` result); the door emits a ``deny`` audit event.
    Fails today because the door cannot complete the handshake at all.
    """
    enrolled_did = _member_did(generate_keypair().public_key)

    caller_kp = generate_keypair()
    caller_did = _member_did(caller_kp.public_key)
    request = _signed_request(caller_did, caller_kp, {})

    door_sink = _RecordingSink()
    door = _build_door(
        tier="federal",
        sink=door_sink,
        agent_did=enrolled_did,
        enrolled=frozenset({enrolled_did}),
    )

    async with _sdk_session(door, present_client_certificate=True) as session:
        refused = False
        try:
            result = await session.call_tool(_TOOL, {}, meta=_arc_meta(request))
            refused = bool(result.isError)
        except McpError:
            refused = True

    assert refused, "an unenrolled caller must be refused at federal"
    assert any(event.outcome == "deny" for event in door_sink.events)


# --- Carrier proof (green control): the ``_meta`` round-trip is real -----------


@pytest.mark.asyncio
async def test_meta_carrier_round_trips_arc_envelope_keys() -> None:
    """GREEN CONTROL: ``arc/*`` keys in ``call_tool(meta=...)`` reach the server.

    Settles the carrier empirically against a scratch REAL SDK server: the envelope
    placed in ``_meta`` is retrievable server-side from ``request_context.meta``
    (as ``model_extra``), so T-1146 reads the Arc envelope from ``_meta`` — headers
    are not needed. This passes on today's SDK (``mcp==1.29.0``); it guards the
    carrier the two RED tests above depend on.
    """
    server = FastMCP(name="carrier-probe")
    captured: dict[str, Any] = {}

    @server.tool(description="probe")
    def probe(text: str, ctx: Context) -> str:  # type: ignore[type-arg]
        meta = ctx.request_context.meta
        captured["extra"] = dict(meta.model_extra or {}) if meta is not None else {}
        return f"echo: {text}"

    envelope = {
        "arc/callerDid": "did:arc:acme:exec/abc",
        "arc/publicKey": "cHVia2V5",
        "arc/signature": "c2ln",
        "arc/nonce": "nonce-1",
        "arc/ts": _now(),
    }

    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        await session.call_tool("probe", {"text": "hi"}, meta=envelope)

    assert captured["extra"] == envelope


# --- Transport guards preserved (REQ-452), driven with raw ASGI scopes ---------


async def _drive(
    door: HttpDoor, scope: dict[str, Any], events: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Run one ASGI request against ``door`` with a hand-built scope; return sends."""
    queue = list(events or [{"type": "http.request", "body": b"", "more_body": False}])

    async def receive() -> dict[str, Any]:
        if queue:
            return queue.pop(0)
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    # Local receive/send use dict scopes; the ASGI protocol types MutableMapping.
    await door(scope, receive, send)  # type: ignore[arg-type]
    return sent


def _status(sent: list[dict[str, Any]]) -> int:
    return int(next(m["status"] for m in sent if m["type"] == "http.response.start"))


@pytest.mark.asyncio
async def test_federal_certless_request_is_still_refused() -> None:
    """GUARD (REQ-452): the federal mTLS gate survives the SDK server integration."""
    door = _build_door(
        tier="federal",
        sink=_RecordingSink(),
        agent_did=_member_did(generate_keypair().public_key),
        enrolled=frozenset(),
    )
    body = b'{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}'
    events = [{"type": "http.request", "body": body, "more_body": False}]

    sent = await _drive(door, {"type": "http", "method": "POST", "headers": []}, events)

    assert _status(sent) == 403


@pytest.mark.asyncio
async def test_oversized_body_is_still_refused() -> None:
    """GUARD (REQ-452): the body cap survives the SDK server integration."""
    door = _build_door(
        tier="personal",
        sink=_RecordingSink(),
        agent_did=_member_did(generate_keypair().public_key),
    )
    over = str(_EIGHT_MIB + 1).encode()

    sent = await _drive(
        door, {"type": "http", "method": "POST", "headers": [(b"content-length", over)]}
    )

    assert _status(sent) == 413


def _one_allow(sink: _RecordingSink) -> AuditEvent:
    """The single ``allow`` audit event a successful dispatch emits."""
    allows = [event for event in sink.events if event.outcome == "allow"]
    assert len(allows) == 1, f"expected exactly one allow event, got {len(allows)}"
    return allows[0]


# --- SECURITY / ERROR guards on the SDK server itself ---------------------------


@pytest.mark.asyncio
async def test_malformed_signature_encoding_fails_closed_and_audits_deny() -> None:
    """A non-base64 ``arc/signature`` decodes to empty and the call is refused.

    Pins ``_b64`` fail-closed (lines ~159-160): a garbage encoding in the identity
    envelope must decode to empty bytes so verification fails inside the pipeline —
    the handler surfaces an ``isError`` refusal and a ``deny`` is audited, never a
    decode error escaping the handler. Distinct from the abuse test, which sends a
    VALID base64 signature signed over different content.
    """
    kp = generate_keypair()
    did = _member_did(kp.public_key)
    request = _signed_request(did, kp, {})
    meta = _arc_meta(request)
    meta["arc/signature"] = "not!valid!base64!"  # raises in base64.b64decode

    door_sink = _RecordingSink()
    door = _build_door(tier="personal", sink=door_sink, agent_did=did)

    async with _sdk_session(door) as session:
        refused = False
        try:
            result = await session.call_tool(_TOOL, {}, meta=meta)
            refused = bool(result.isError)
        except McpError:
            refused = True

    assert refused, "a malformed signature encoding must fail closed"
    assert any(event.outcome == "deny" for event in door_sink.events)
    assert not any(event.outcome == "allow" for event in door_sink.events)


@pytest.mark.asyncio
async def test_call_tool_is_read_only_when_a_collaborator_is_missing() -> None:
    """With ``provider=None`` the door lists but refuses ``tools/call`` without dispatch.

    Pins the read-only guard (line ~97): when any of provider/allowlist/replay_cache/
    audit_sink is absent the handler returns the "tools/call is not enabled" error and
    performs NO dispatch and NO audit — the recording sink stays empty.
    """
    from arcagent.modules.mcp_server.sdk_server import build_sdk_server

    sink = _RecordingSink()
    server = build_sdk_server(
        McpServer(_FakeRegistry({_TOOL: _FakeTool(_TOOL)})),  # type: ignore[arg-type]
        tier="personal",
        provider=None,
        allowlist=_allowlist("personal"),
        replay_cache=ReplayCache(),
        audit_sink=sink,
    )

    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool(_TOOL, {})

    assert result.isError
    assert "tools/call is not enabled" in result.content[0].text  # type: ignore[union-attr]
    assert sink.events == []
