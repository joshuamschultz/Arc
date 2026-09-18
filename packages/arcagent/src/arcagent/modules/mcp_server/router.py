"""SPEC-082 T-1110 / COMP-001 — the transport-agnostic JSON-RPC handler.

The door answers three MCP methods — ``server/discover``, ``tools/list``
(allowlist-filtered), and ``tools/call`` (verify → authorize → dispatch → audit).
That message logic is identical over every transport, so it lives here as
:class:`DoorRouter`, a handler with one async method
``handle(message: dict) -> dict`` that returns the full JSON-RPC response
envelope. :class:`~arcagent.modules.mcp_server.http_transport.HttpDoor` and
:func:`~arcagent.modules.mcp_server.stdio_transport.serve_stdio` both drive this
same router, so the two transports stay byte-for-byte identical on the wire.

Transport concerns (HTTP status codes, mTLS, body caps; stdio framing) stay in
the transports. This module never touches a socket or a stream.
"""

from __future__ import annotations

import base64
from collections.abc import Collection
from typing import Any

from arcteam.crypto import ReplayCache
from arctrust import AuditSink

from arcagent.capabilities.provider import AgentCapabilityProvider
from arcagent.modules.mcp_server.allowlist import AllowlistRefused, ExposureAllowlist
from arcagent.modules.mcp_server.door import authorize_and_dispatch
from arcagent.modules.mcp_server.identity import InboundRejected, InboundRequest
from arcagent.modules.mcp_server.server import McpServer

#: JSON-RPC error codes used by the door's message surface.
METHOD_NOT_FOUND = -32601
INVALID_REQUEST = -32600
ACCESS_DENIED = -32001
CALL_UNAVAILABLE = -32002

#: ``_meta`` keys carrying the inbound signed envelope on a ``tools/call``.
_META_CALLER_DID = "arc/callerDid"
_META_PUBLIC_KEY = "arc/publicKey"
_META_SIGNATURE = "arc/signature"
_META_NONCE = "arc/nonce"
_META_TS = "arc/ts"


class DoorRouter:
    """Answer the MCP methods from an :class:`McpServer` and its trust collaborators.

    ``tools/call`` is served only when ``provider``, ``allowlist``,
    ``replay_cache``, and ``audit_sink`` are all supplied; otherwise the router is
    a read-only listing surface and a ``tools/call`` returns a JSON-RPC error.
    """

    def __init__(
        self,
        server: McpServer,
        *,
        tier: str = "personal",
        provider: AgentCapabilityProvider | None = None,
        allowlist: ExposureAllowlist | None = None,
        replay_cache: ReplayCache | None = None,
        audit_sink: AuditSink | None = None,
        enrolled: Collection[str] | None = None,
    ) -> None:
        self._server = server
        self._tier = tier
        self._provider = provider
        self._allowlist = allowlist
        self._replay_cache = replay_cache
        self._audit_sink = audit_sink
        self._enrolled = enrolled

    async def handle(self, message: dict[str, Any]) -> dict[str, Any]:
        """Route one JSON-RPC message to its full response envelope."""
        method = message.get("method")
        msg_id = message.get("id")
        if method == "server/discover":
            return _result(msg_id, self._server.discover())
        if method == "tools/list":
            return _result(msg_id, self._list(message))
        if method == "tools/call":
            return await self._call(message, msg_id)
        return _error(msg_id, METHOD_NOT_FOUND, f"method not found: {method!r}")

    def _list(self, message: dict[str, Any]) -> dict[str, Any]:
        cursor = message.get("params", {}).get("cursor")
        page = self._server.list_tools(cursor=cursor)
        tools = page.tools
        if self._allowlist is not None:
            exposed = set(self._allowlist.filter(tool["name"] for tool in tools))
            tools = [tool for tool in tools if tool["name"] in exposed]
        result: dict[str, Any] = {"tools": tools}
        if page.next_cursor is not None:
            result["nextCursor"] = page.next_cursor
        return result

    async def _call(self, message: dict[str, Any], msg_id: Any) -> dict[str, Any]:
        if (
            self._provider is None
            or self._allowlist is None
            or self._replay_cache is None
            or self._audit_sink is None
        ):
            return _error(msg_id, CALL_UNAVAILABLE, "tools/call is not enabled on this door")
        request = _inbound_request(message)
        try:
            result = await authorize_and_dispatch(
                request,
                provider=self._provider,
                allowlist=self._allowlist,
                replay_cache=self._replay_cache,
                audit_sink=self._audit_sink,
                tier=self._tier,
                enrolled=self._enrolled,
            )
        except (InboundRejected, AllowlistRefused) as exc:
            return _error(msg_id, ACCESS_DENIED, str(exc))
        return _result(
            msg_id,
            {"content": [{"type": "text", "text": result.content}], "isError": result.is_error},
        )


def _inbound_request(message: dict[str, Any]) -> InboundRequest:
    """Rebuild the signed envelope a ``tools/call`` carries in ``params._meta``."""
    params = message.get("params", {})
    meta = params.get("_meta", {})
    name = params.get("name", "")
    arguments = params.get("arguments", {})
    content = {"method": message.get("method"), "params": {"name": name, "arguments": arguments}}
    return InboundRequest(
        caller_did=meta.get(_META_CALLER_DID, ""),
        public_key=_b64(meta.get(_META_PUBLIC_KEY, "")),
        signature=_b64(meta.get(_META_SIGNATURE, "")),
        content=content,
        nonce=meta.get(_META_NONCE, ""),
        ts=meta.get(_META_TS, ""),
    )


def _b64(value: str) -> bytes:
    """Decode a base64 wire field, treating garbage as empty (fails closed later)."""
    try:
        return base64.b64decode(value)
    except (ValueError, TypeError):
        return b""


def _result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


__all__ = ["DoorRouter"]
