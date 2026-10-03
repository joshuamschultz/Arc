"""Standing approvals — the operator's "Always allow" rows (SPEC-035 OQ-3, 2026-10-03).

``GET  /api/standing-grants``             — list (any authed role). Filters:
                                             ``agent_did``, ``status`` (``active``
                                             default, ``revoked``, ``all``).
``POST /api/standing-grants/{id}/revoke`` — revoke at once (operator only).

Rows are created by ``POST /api/approvals/{id}/always``. The signed grant itself
never leaves the server — the list carries scope, grantor, time and use count.
A revoke takes effect on the agent's next call: the gate reads active rows fresh
on every hit.
"""

from __future__ import annotations

import logging
from typing import Any

from arcstore.standing_grants import StandingGrant, StandingGrantStore
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit
from arcui.schemas import ErrorResponse

logger = logging.getLogger("arcui.routes.standing_grants")

_OPERATOR_DID = "did:arc:ui:operator"
_STATUSES = {"active", "revoked"}


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


def standing_store(request: Request) -> StandingGrantStore:
    """The deployment's standing-grant store (same backend as approvals)."""
    store: StandingGrantStore = request.app.state.standing_grant_store
    return store


def grant_row(grant: StandingGrant) -> dict[str, Any]:
    """One standing grant as the panel shows it — never the signature."""
    row = grant.model_dump(mode="json")
    row.pop("grant", None)
    return row


async def list_standing_grants(request: Request) -> JSONResponse:
    """GET /api/standing-grants — scope, grantor, when, and use count."""
    status = request.query_params.get("status", "active")
    if status != "all" and status not in _STATUSES:
        return _error("unknown_status", 400)
    agent_did = request.query_params.get("agent_did") or None
    try:
        grants = await standing_store(request).list(
            agent_did=agent_did,
            status=None if status == "all" else status,  # type: ignore[arg-type]  # checked above
        )
    except Exception:  # reason: a saturated pool must degrade, not 500 the panel
        logger.exception("standing grants read failed")
        return _error("standing approvals temporarily unavailable", 503)
    return JSONResponse({"grants": [grant_row(g) for g in grants]})


async def revoke_standing_grant(request: Request) -> JSONResponse:
    """POST /api/standing-grants/{id}/revoke — operator only, effective at once."""
    grant_id = request.path_params["id"]
    target = f"standing_grant:{grant_id}"
    if getattr(request.state, "role", None) != "operator":
        emit_mutation_audit(
            request,
            target=target,
            operation="standing_grant.revoke",
            outcome="denied",
            detail="viewer role",
        )
        return _error("operator_role_required", 403)
    revoked = await standing_store(request).revoke(grant_id, actor_did=_OPERATOR_DID)
    if revoked is None:
        return _error("standing_grant_not_active", 404)
    emit_mutation_audit(
        request,
        target=target,
        operation="standing_grant.revoke",
        outcome="applied",
        detail=f"{revoked.agent_did} {revoked.tool} -> {revoked.destination or '(no egress)'}",
    )
    return JSONResponse(grant_row(revoked))


routes = [
    Route("/api/standing-grants", list_standing_grants, methods=["GET"]),
    Route("/api/standing-grants/{id}/revoke", revoke_standing_grant, methods=["POST"]),
]

__all__ = [
    "grant_row",
    "list_standing_grants",
    "revoke_standing_grant",
    "routes",
    "standing_store",
]
