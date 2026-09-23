"""SPEC-084 T-1146 / COMP-002 — serve the Arc MCP door through the official ``mcp`` SDK.

The door is served by an ``mcp`` low-level :class:`~mcp.server.lowlevel.Server` so a
genuine :class:`mcp.ClientSession` can complete the real MCP handshake
(``initialize`` / ``tools/list`` / ``tools/call``) against it — the hand-rolled
``server/discover`` dialect is gone. This module is a thin adapter and owns no
policy of its own:

- ``list_tools`` returns the allowlist-filtered catalog read from the read-only
  :class:`~arcagent.modules.mcp_server.server.McpServer` (listing behaviour
  unchanged).
- ``call_tool`` reconstructs the Arc signed envelope from the SDK request
  ``_meta`` (the settled carrier — never headers) and runs the UNCHANGED SPEC-082
  pipeline :func:`~arcagent.modules.mcp_server.door.authorize_and_dispatch`, so the
  policy decision and WORM audit record are byte-identical to the native path. The
  signed ``content`` is reconstructed EXACTLY as ``{"method": "tools/call",
  "params": {"name": <tool>, "arguments": <args>}}`` so the signature verifies over
  the same canonical bytes.

Input validation is disabled on the SDK ``call_tool`` handler on purpose: the
door's own verify → enroll → allowlist → dispatch → audit pipeline is the sole
authority, and every inbound call must reach it (and be audited) rather than being
pre-empted by the SDK's JSON-Schema check.
"""

from __future__ import annotations

import base64
from collections.abc import Collection
from typing import Any

import mcp.types as mcp_types
from arctrust import AuditSink, ReplayCache
from mcp.server.lowlevel import Server

from arcagent import __version__
from arcagent.capabilities.provider import AgentCapabilityProvider
from arcagent.modules.mcp_server.allowlist import AllowlistRefused, ExposureAllowlist
from arcagent.modules.mcp_server.door import authorize_and_dispatch
from arcagent.modules.mcp_server.identity import InboundRejected, InboundRequest
from arcagent.modules.mcp_server.server import McpServer

#: ``_meta`` keys carrying the inbound signed envelope on a ``tools/call``.
_META_CALLER_DID = "arc/callerDid"
_META_PUBLIC_KEY = "arc/publicKey"
_META_SIGNATURE = "arc/signature"
_META_NONCE = "arc/nonce"
_META_TS = "arc/ts"

#: The JSON-RPC method whose canonical content the door reconstructs and verifies.
_CALL_METHOD = "tools/call"


def build_sdk_server(
    mcp_server: McpServer,
    *,
    server_name: str = "arc",
    tier: str = "personal",
    provider: AgentCapabilityProvider | None = None,
    allowlist: ExposureAllowlist | None = None,
    replay_cache: ReplayCache | None = None,
    audit_sink: AuditSink | None = None,
    enrolled: Collection[str] | None = None,
) -> Server[Any, Any]:
    """Build an ``mcp`` low-level Server serving the door over the real protocol.

    ``tools/call`` is served only when ``provider``, ``allowlist``, ``replay_cache``,
    and ``audit_sink`` are all supplied; otherwise the server is a read-only listing
    surface and a ``tools/call`` yields an ``isError`` result.
    """
    server: Server[Any, Any] = Server(name=server_name, version=__version__)

    @server.list_tools()
    async def _list_tools() -> list[mcp_types.Tool]:
        entries = _catalog(mcp_server)
        if allowlist is not None:
            exposed = set(allowlist.filter(entry["name"] for entry in entries))
            entries = [entry for entry in entries if entry["name"] in exposed]
        return [
            mcp_types.Tool(
                name=entry["name"],
                description=entry["description"],
                inputSchema=entry["inputSchema"],
            )
            for entry in entries
        ]

    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: dict[str, Any]) -> mcp_types.CallToolResult:
        if (
            provider is None
            or allowlist is None
            or replay_cache is None
            or audit_sink is None
        ):
            return _error_result("tools/call is not enabled on this door")
        request = _inbound_from_meta(server, name, arguments)
        try:
            result = await authorize_and_dispatch(
                request,
                provider=provider,
                allowlist=allowlist,
                replay_cache=replay_cache,
                audit_sink=audit_sink,
                tier=tier,
                enrolled=enrolled,
            )
        except (InboundRejected, AllowlistRefused) as exc:
            # Fail closed: the deny is already audited inside the pipeline; the SDK
            # surfaces the refusal to the client as an ``isError`` result.
            return _error_result(str(exc))
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text=result.content)],
            isError=result.is_error,
        )

    return server


def _catalog(mcp_server: McpServer) -> list[dict[str, Any]]:
    """Every tool descriptor across all catalog pages, in the server's stable order."""
    entries: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page = mcp_server.list_tools(cursor=cursor)
        entries.extend(page.tools)
        if page.next_cursor is None:
            return entries
        cursor = page.next_cursor


def _inbound_from_meta(
    server: Server[Any, Any], name: str, arguments: dict[str, Any]
) -> InboundRequest:
    """Rebuild the signed envelope a ``tools/call`` carries in the SDK ``_meta``.

    The envelope rides in ``request_context.meta`` (a ``RequestParams.Meta`` whose
    ``model_extra`` holds the ``arc/*`` keys). ``content`` is reconstructed to the
    exact canonical shape the caller signed so the signature verifies unchanged.
    """
    meta = server.request_context.meta
    extra: dict[str, Any] = dict(meta.model_extra or {}) if meta is not None else {}
    content = {"method": _CALL_METHOD, "params": {"name": name, "arguments": arguments}}
    return InboundRequest(
        caller_did=extra.get(_META_CALLER_DID, ""),
        public_key=_b64(extra.get(_META_PUBLIC_KEY, "")),
        signature=_b64(extra.get(_META_SIGNATURE, "")),
        content=content,
        nonce=extra.get(_META_NONCE, ""),
        ts=extra.get(_META_TS, ""),
    )


def _b64(value: str) -> bytes:
    """Decode a base64 wire field, treating garbage as empty (fails closed later)."""
    try:
        return base64.b64decode(value)
    except (ValueError, TypeError):
        return b""


def _error_result(message: str) -> mcp_types.CallToolResult:
    """A refusal, surfaced to the SDK client as an ``isError`` tool result."""
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=message)],
        isError=True,
    )


__all__ = ["build_sdk_server"]
