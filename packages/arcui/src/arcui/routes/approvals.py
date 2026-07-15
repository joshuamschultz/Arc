"""Operator-gated approval surface — the arcui half of mechanical HITL (SPEC-035).

``GET  /api/approvals``              — list pending trifecta-block requests (any role).
``POST /api/approvals/{id}/approve`` — mint an operator-signed grant (operator only).
``POST /api/approvals/{id}/deny``    — deny the request (operator only).

Approval never rides on agent chat (forgeable); it is an operator-role action that
attaches an operator-signed grant the agent's gate verifies AND pins to the
deployment operator DID. arcui runs on the box, so it signs with the same
``~/.arc/operator`` key the agent pins to — a viewer session or a foreign process
cannot mint it.
"""

from __future__ import annotations

import logging

from arcstore.approvals import ApprovalStore
from arctrust import OperatorKey, default_operator_key_path
from arctrust.policy import OperatorApprovalAuthority, grant_to_wire, sign_approval_for_hash
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit
from arcui.schemas import ErrorResponse

logger = logging.getLogger("arcui.routes.approvals")

_OPERATOR_DID = "did:arc:ui:operator"


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


def _is_operator(request: Request) -> bool:
    return getattr(request.state, "role", None) == "operator"


def _store(request: Request) -> ApprovalStore:
    store: ApprovalStore = request.app.state.approval_store
    return store


def _operator_authority() -> OperatorApprovalAuthority:
    """The deployment operator approval authority (on-box ~/.arc/operator key).

    Read-only load — never bootstraps a key here (an unpinned operator is no
    operator); a missing key raises and the caller fails the approve with 500.
    """
    signer = OperatorKey.load(default_operator_key_path(), generate_if_absent=False).into_signer()
    return OperatorApprovalAuthority(signer)


async def list_approvals(request: Request) -> JSONResponse:
    """GET /api/approvals — pending requests (visible to any authed role)."""
    pending = await _store(request).list(status="pending")
    return JSONResponse({"approvals": [a.model_dump(mode="json") for a in pending]})


async def approve_request(request: Request) -> JSONResponse:
    """POST /api/approvals/{id}/approve — mint + attach an operator grant."""
    approval_id = request.path_params["id"]
    target = f"approval:{approval_id}"
    if not _is_operator(request):
        emit_mutation_audit(
            request, target=target, operation="approval.approve",
            outcome="denied", detail="viewer role",
        )
        return _error("operator_role_required", 403)

    store = _store(request)
    row = await store.get(approval_id)
    if row is None:
        return _error("not found", 404)
    if row.status != "pending":
        return _error("approval_not_pending", 409)

    try:
        operator = _operator_authority()
    except (FileNotFoundError, OSError) as exc:
        logger.exception("operator key unavailable for approval")
        emit_mutation_audit(
            request, target=target, operation="approval.approve",
            outcome="denied", detail="operator key unavailable",
        )
        return _error(f"operator_key_unavailable: {type(exc).__name__}", 500)

    grant = sign_approval_for_hash(row.call_hash, operator)
    updated = await store.resolve(
        approval_id, status="approved", actor_did=operator.did,
        resolved_by=operator.did, grant=grant_to_wire(grant),
    )
    if updated is None:
        return _error("approval_not_pending", 409)
    emit_mutation_audit(request, target=target, operation="approval.approve", outcome="applied")
    return JSONResponse(updated.model_dump(mode="json"))


async def deny_request(request: Request) -> JSONResponse:
    """POST /api/approvals/{id}/deny — deny the request (operator only)."""
    approval_id = request.path_params["id"]
    target = f"approval:{approval_id}"
    if not _is_operator(request):
        emit_mutation_audit(
            request, target=target, operation="approval.deny",
            outcome="denied", detail="viewer role",
        )
        return _error("operator_role_required", 403)

    store = _store(request)
    if await store.get(approval_id) is None:
        return _error("not found", 404)
    updated = await store.resolve(
        approval_id, status="denied", actor_did=_OPERATOR_DID, resolved_by=_OPERATOR_DID
    )
    if updated is None:
        return _error("approval_not_pending", 409)
    emit_mutation_audit(request, target=target, operation="approval.deny", outcome="applied")
    return JSONResponse(updated.model_dump(mode="json"))


routes = [
    Route("/api/approvals", list_approvals, methods=["GET"]),
    Route("/api/approvals/{id}/approve", approve_request, methods=["POST"]),
    Route("/api/approvals/{id}/deny", deny_request, methods=["POST"]),
]

__all__ = ["approve_request", "deny_request", "list_approvals", "routes"]
