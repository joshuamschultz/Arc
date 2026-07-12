"""``POST /api/agents/{agent_id}/sessions/new`` — start a fresh session.

Rotates the caller's (agent, user) session via the gateway's SessionRouter so
the next message begins an empty conversation — the web equivalent of the
``/new`` slash command. Allowed for viewer and operator alike: it rotates the
*caller's own* conversation, not a fleet-wide resource, so it is not gated to
operators (but is still audited).

Returns ``201 {"session_key": "<new key>"}``. The chat WebSocket re-derives the
current key on its next connect (the ``ready`` frame carries it), so the client
just clears its transcript and reconnects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from arcgateway.identity import derive_viewer_did
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit
from arcui.schemas import ErrorResponse

if TYPE_CHECKING:
    from arcgateway.session import SessionRouter


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


def _resolve_agent_did(request: Request, agent_id: str) -> str | None:
    """Find the agent DID for a roster id/name, or None if unknown."""
    roster_provider = getattr(request.app.state, "roster_provider", None)
    if roster_provider is None:
        return None
    for entry in roster_provider():
        if agent_id in (getattr(entry, "agent_id", None), getattr(entry, "name", None)):
            return getattr(entry, "did", None) or ""
    return None


async def new_session(request: Request) -> JSONResponse:
    """POST /api/agents/{agent_id}/sessions/new — rotate the caller's session.

    Auth is enforced upstream by ``AuthMiddleware`` (a valid viewer/operator
    token is required for every ``/api/*`` route). Rotation is the caller's own
    conversation, so both roles may do it — no extra operator gate here.
    """
    agent_id = request.path_params["agent_id"]
    agent_did = _resolve_agent_did(request, agent_id)
    if not agent_did:
        return _error("not found", 404)

    router: SessionRouter | None = getattr(request.app.state, "session_router", None)
    if router is None:
        return _error("chat is not enabled on this server", 503)

    # Same user_did derivation the chat WebSocket uses, so we rotate the exact
    # (agent, user) pair the socket will resolve on its next connect.
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    user_did = derive_viewer_did(token)
    session_key = router.new_session(agent_did, user_did)

    emit_mutation_audit(
        request, target=f"session:{session_key}", operation="session.rotate", outcome="applied"
    )
    return JSONResponse({"session_key": session_key}, status_code=201)


routes = [
    Route("/api/agents/{agent_id}/sessions/new", new_session, methods=["POST"]),
]
