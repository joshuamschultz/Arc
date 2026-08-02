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
from typing import Any

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


#: The pending-approval ``tool`` an agent writes when it wants a draft signed.
WORKFLOW_SIGN_TOOL = "workflow_sign"


def _sign_requested_workflow(request: Request, row: Any) -> str | None:
    """Sign the bundle this approval was raised for. Returns a refusal, or None.

    The operator key is resolved here, in this process, exactly as it already is
    for the approval grant itself. What must never happen is an agent reaching
    a signature: the agent can only WRITE THE REQUEST, and this path is gated on
    the operator role before it runs.
    """
    workflow_id = str((row.arguments or {}).get("workflow_id", ""))
    if not workflow_id:
        return "workflow_sign_request_names_no_workflow"
    plane = getattr(request.app.state, "workflow_control_plane", None)
    definitions = getattr(plane, "definitions", None)
    if definitions is None:
        return "workflow_control_plane_unavailable"
    try:
        from arcteam.workflow import sign_definition

        bundle = definitions.load(workflow_id)
        if row.call_hash and bundle.content_hash != row.call_hash:
            # The whole point of binding the request to a hash: what the
            # operator read is not what they would be signing.
            return "workflow_changed_since_the_request"
        operator = OperatorKey.load(default_operator_key_path(), generate_if_absent=False)
        seed = getattr(operator, "seed", None)
        if not seed:
            return "operator_key_has_no_in_process_seed"
        sign_definition(
            definitions,
            workflow_id,
            signer_did=f"operator:{operator.public_key.hex()[:16]}",
            private_key=seed,
        )
    except Exception as exc:  # reason: a refusal is reported, never a 500 page
        logger.exception("signing workflow %s from approval failed", workflow_id)
        return f"workflow_sign_failed: {type(exc).__name__}: {exc}"
    return None


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
            request,
            target=target,
            operation="approval.approve",
            outcome="denied",
            detail="viewer role",
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
            request,
            target=target,
            operation="approval.approve",
            outcome="denied",
            detail="operator key unavailable",
        )
        return _error(f"operator_key_unavailable: {type(exc).__name__}", 500)

    # A workflow-signing request is the one approval whose grant is not the
    # whole effect: approving it SIGNS the bundle. The signature happens here,
    # in the operator-authenticated path, and never through the control plane —
    # the operation set an agent can reach must stay one that cannot sign
    # (REQ-224). The row's call_hash is the definition's content hash, so a
    # definition edited since the request no longer matches and is refused.
    if row.tool == WORKFLOW_SIGN_TOOL:
        signed = _sign_requested_workflow(request, row)
        if signed is not None:
            emit_mutation_audit(
                request,
                target=target,
                operation="approval.approve",
                outcome="denied",
                detail=signed,
            )
            return _error(signed, 409)

    grant = sign_approval_for_hash(row.call_hash, operator)
    updated = await store.resolve(
        approval_id,
        status="approved",
        actor_did=operator.did,
        resolved_by=operator.did,
        grant=grant_to_wire(grant),
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
            request,
            target=target,
            operation="approval.deny",
            outcome="denied",
            detail="viewer role",
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
