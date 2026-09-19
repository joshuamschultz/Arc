"""SPEC-082 COMP-001 / REQ-418 — the streamable-HTTP door, served by the ``mcp`` SDK.

``HttpDoor`` is the ASGI shell that owns the transport GUARDS; the MCP wire itself is
served by the official ``mcp`` SDK. It is a plain ASGI callable — no server framework
— so it runs under any ASGI server and is exercised in tests through
``httpx.ASGITransport`` or a hand-built scope.

The shell owns three concerns, all enforced BEFORE the SDK ever sees the request:

- **mTLS at enterprise and federal.** A request without a client certificate in the
  ASGI ``tls`` extension is refused 403 (REQ-418) — tier is stringency, and both
  hardened tiers require the client cert.
- **Method.** Only ``POST`` carries a message; anything else is refused 405.
- **Body cap (declared).** A declared ``content-length`` over the cap is refused 413
  before the body is read — a large read is a memory-exhaustion primitive (LLM10).
  The cap is also handed to the SDK manager, whose own body-limit middleware refuses a
  streamed body that overflows the cap mid-request (defense in depth).

Once the guards pass, the request is delegated UNCHANGED to a per-request
:class:`~mcp.server.streamable_http_manager.StreamableHTTPSessionManager` (stateless,
JSON responses) driving the door's SDK :class:`~mcp.server.lowlevel.Server`. A fresh
manager per request keeps this correct whether the door is long-lived (``arc mcp
serve --http`` under uvicorn) or rebuilt per request (the arcui ``/mcp`` mount) — the
SDK manager's ``run()`` context may only be entered once per instance, and stateless
mode carries no state between requests, so a new instance per request is the simplest
correct wiring.
"""

from __future__ import annotations

import json
from collections.abc import Collection

from arcteam.crypto import ReplayCache
from arctrust import AuditSink
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.types import Receive, Scope, Send

from arcagent.capabilities.provider import AgentCapabilityProvider
from arcagent.modules.mcp_server.allowlist import ExposureAllowlist
from arcagent.modules.mcp_server.sdk_server import build_sdk_server
from arcagent.modules.mcp_server.server import McpServer

_DEFAULT_MAX_BODY_BYTES = 8 * 1024 * 1024

#: Tiers whose door requires an mTLS client certificate on the HTTP transport.
_MTLS_TIERS = frozenset({"enterprise", "federal"})

#: JSON-RPC error codes for the shell's own transport-guard refusals.
_INVALID_REQUEST = -32600
_ACCESS_DENIED = -32001


class HttpDoor:
    """A streamable-HTTP ASGI shell whose guards front the ``mcp`` SDK transport.

    ``tools/call`` is served only when ``provider``, ``allowlist``, ``replay_cache``,
    and ``audit_sink`` are all supplied; otherwise the door is a read-only listing
    surface.
    """

    def __init__(
        self,
        server: McpServer,
        *,
        server_name: str = "arc",
        tier: str = "personal",
        max_body_bytes: int = _DEFAULT_MAX_BODY_BYTES,
        provider: AgentCapabilityProvider | None = None,
        allowlist: ExposureAllowlist | None = None,
        replay_cache: ReplayCache | None = None,
        audit_sink: AuditSink | None = None,
        enrolled: Collection[str] | None = None,
    ) -> None:
        self._tier = tier
        self._max_body_bytes = max_body_bytes
        self._sdk_server = build_sdk_server(
            server,
            server_name=server_name,
            tier=tier,
            provider=provider,
            allowlist=allowlist,
            replay_cache=replay_cache,
            audit_sink=audit_sink,
            enrolled=enrolled,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope.get("type")
        if scope_type == "lifespan":
            await _drain_lifespan(receive, send)
            return
        if scope_type != "http":
            return
        if scope.get("method") != "POST":
            await _refuse(send, 405, _INVALID_REQUEST, "only POST is accepted")
            return
        if self._tier in _MTLS_TIERS and not _has_client_certificate(scope):
            await _refuse(send, 403, _ACCESS_DENIED, "mTLS client certificate required")
            return
        if _content_length(scope) > self._max_body_bytes:
            await _refuse(send, 413, _INVALID_REQUEST, "request body too large")
            return

        # Guards passed: hand the raw request to a fresh SDK manager. Stateless +
        # JSON responses means each request is self-contained, so a per-request
        # instance is correct and sidesteps the "run() once per instance" rule.
        manager = StreamableHTTPSessionManager(
            app=self._sdk_server,
            stateless=True,
            json_response=True,
            max_request_body_size=self._max_body_bytes,
        )
        async with manager.run():
            await manager.handle_request(scope, receive, send)


async def _refuse(send: Send, status: int, code: int, message: str) -> None:
    """Emit a JSON-RPC error envelope for a transport-guard refusal."""
    payload = {"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}}
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _has_client_certificate(scope: Scope) -> bool:
    """Whether the ASGI ``tls`` extension carries a client certificate chain."""
    tls = scope.get("extensions", {}).get("tls")
    if not tls:
        return False
    return bool(tls.get("client_cert_chain"))


def _content_length(scope: Scope) -> int:
    """The declared ``content-length``, or 0 when absent/unparseable."""
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return 0
    return 0


async def _drain_lifespan(receive: Receive, send: Send) -> None:
    """Answer the ASGI lifespan protocol so a hosting server can start/stop cleanly."""
    while True:
        event = await receive()
        event_type = event.get("type")
        if event_type == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif event_type == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


__all__ = ["HttpDoor"]
