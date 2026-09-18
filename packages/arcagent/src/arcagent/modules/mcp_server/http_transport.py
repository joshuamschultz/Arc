"""SPEC-082 COMP-001 / REQ-418 — the streamable-HTTP door responder.

The door's HTTP surface is a raw ASGI app: one POST per message, answering JSON,
capped at 8 MiB, serving ``server/discover`` / ``tools/list`` / ``tools/call``. It
is a plain ASGI callable — no server framework, no vendor SDK (CON-7) — so it runs
under any ASGI server and is exercised in tests through ``httpx.ASGITransport`` or a
hand-built scope.

The JSON-RPC *message* logic (which method, allowlist filtering, verify →
dispatch → audit) lives in :class:`~arcagent.modules.mcp_server.router.DoorRouter`,
shared with the stdio transport. This module owns only the HTTP concerns:

- **mTLS at enterprise and federal.** A request that arrives without a client
  certificate in the ASGI ``tls`` extension is refused before anything is served
  (REQ-418) — tier is stringency, and both hardened tiers require the client cert.
- **Body cap.** A body over ``max_body_bytes`` is refused with 413 before it is
  parsed — a large read is a memory-exhaustion primitive (LLM10).
- **HTTP status mapping.** The router's JSON-RPC envelope maps to a status code
  (access-denied → 403, other errors → 400, results → 200).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Collection
from typing import Any

from arcteam.crypto import ReplayCache
from arctrust import AuditSink

from arcagent.capabilities.provider import AgentCapabilityProvider
from arcagent.modules.mcp_server.allowlist import ExposureAllowlist
from arcagent.modules.mcp_server.router import (
    ACCESS_DENIED,
    INVALID_REQUEST,
    DoorRouter,
    _error,
)
from arcagent.modules.mcp_server.server import McpServer

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]

_DEFAULT_MAX_BODY_BYTES = 8 * 1024 * 1024

#: Tiers whose door requires an mTLS client certificate on the HTTP transport.
_MTLS_TIERS = frozenset({"enterprise", "federal"})


class HttpDoor:
    """A streamable-HTTP ASGI responder that delegates message logic to a router.

    ``tools/call`` is served only when ``provider``, ``allowlist``,
    ``replay_cache``, and ``audit_sink`` are all supplied; otherwise the door is a
    read-only listing surface.
    """

    def __init__(
        self,
        server: McpServer,
        *,
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
        self._router = DoorRouter(
            server,
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
            await self._respond(send, 405, _error(None, INVALID_REQUEST, "only POST is accepted"))
            return
        if self._tier in _MTLS_TIERS and not _has_client_certificate(scope):
            await self._respond(
                send, 403, _error(None, ACCESS_DENIED, "mTLS client certificate required")
            )
            return
        too_large = _error(None, INVALID_REQUEST, "request body too large")
        if _content_length(scope) > self._max_body_bytes:
            await self._respond(send, 413, too_large)
            return

        body = await self._read_body(receive)
        if body is None:
            await self._respond(send, 413, too_large)
            return
        try:
            message = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            await self._respond(send, 400, _error(None, INVALID_REQUEST, "malformed JSON"))
            return

        payload = await self._router.handle(message)
        await self._respond(send, _status_for(payload), payload)

    async def _read_body(self, receive: Receive) -> bytes | None:
        """Accumulate the request body, returning ``None`` if it exceeds the cap."""
        body = b""
        more_body = True
        while more_body:
            event = await receive()
            body += event.get("body", b"")
            if len(body) > self._max_body_bytes:
                return None
            more_body = event.get("more_body", False)
        return body

    async def _respond(self, send: Send, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _status_for(payload: dict[str, Any]) -> int:
    """Map a router JSON-RPC envelope to its HTTP status code."""
    error = payload.get("error")
    if error is None:
        return 200
    return 403 if error.get("code") == ACCESS_DENIED else 400


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
