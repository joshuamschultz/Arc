"""Always-on fleet MCP door, mounted into the arcui Starlette server (SPEC-082).

``arcagent`` is headless and never binds a port, so its per-agent MCP door only
served through a separate CLI process. This mounts the door at ``/mcp`` so it
comes up with ``arc.service`` — no second process.

The mount is a raw ASGI callable (``FleetMcpDoor``). For a request under
``/mcp/{agent_did}`` it:

1. Reads ``agent_did`` as the WHOLE sub-path remainder (the DID itself contains a
   ``/`` — ``did:arc:{org}:{type}/{hash}`` — so it is never split on ``/``). Under
   a Starlette ``Mount`` the matched prefix moves to ``scope["root_path"]``; the
   sub-path is ``scope["path"]`` with that ``root_path`` removed.
2. Resolves the live agent from ``app.state.embedded_agent_cache``; a missing
   cache or unknown DID → 404 (never a 500 when the embedded gateway is unwired).
3. Builds the door through the ``arcagent`` facade (lazy-imports the optional
   module); a disabled door raises ``ValueError`` → 404.
4. Delegates the RAW ASGI request UNCHANGED to ``built.http_app`` so the door's
   own status codes (200/403/405/413) and its enterprise/federal mTLS check are
   preserved — method, body and tls pass through untouched.

``/mcp`` is intentionally NOT behind ``AuthMiddleware`` (which skips non-``/api/``
paths): the door authenticates via its own signed envelope, not the dashboard's
viewer/operator tokens.
"""

from __future__ import annotations

import json

import arcagent
from starlette.types import Receive, Scope, Send


class FleetMcpDoor:
    """ASGI app mounted at ``/mcp`` that resolves an agent and delegates to its door."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            return

        # Starlette's Mount puts the matched "/mcp" prefix on root_path and leaves
        # the full path in place; the sub-path is path minus root_path. The DID
        # itself contains a "/", so take the whole remainder — never split it.
        path = scope.get("path", "")
        root_path = scope.get("root_path", "")
        if root_path and path.startswith(root_path):
            path = path[len(root_path) :]
        agent_did = path.lstrip("/")

        cache = getattr(scope["app"].state, "embedded_agent_cache", None)
        if cache is None:
            await _not_found(send)
            return
        agent = cache.get(agent_did)
        if agent is None:
            await _not_found(send)
            return

        try:
            built = arcagent.build_mcp_door(agent)
        except ValueError:
            # [modules.mcp_server] disabled for this agent — no door to serve.
            await _not_found(send)
            return

        # Delegate the raw request untouched: the door owns method/body/tls and
        # answers with its own status codes (200/403/405/413).
        await built.http_app(scope, receive, send)


async def _not_found(send: Send) -> None:
    """Emit a minimal ASGI JSON 404 without touching the door."""
    body = json.dumps({"error": "Not Found"}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 404,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


__all__ = ["FleetMcpDoor"]
