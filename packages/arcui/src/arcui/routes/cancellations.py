"""Operator kill-switch surface — the arcui half of run cancellation.

``GET  /api/cancellations`` — list pending cancel requests (any authed role).
``POST /api/cancellations`` — request cancellation of a running run (operator only).

Surfaces run in a *separate process* from the agent, so this route cannot stop a
run directly — it parks a ``pending`` row in the shared arcstore ``cancellations``
directory, attributed to the operator, that the target agent's run-control watcher
observes and applies (``RunHandle.cancel``). Cancellation is an operator-role action
(ASI09/ASI10); a viewer session is refused and the refusal is audited.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from arcstore.cancellations import CancelRequest, CancelStore
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit
from arcui.schemas import ErrorResponse

logger = logging.getLogger("arcui.routes.cancellations")

_OPERATOR_DID = "did:arc:ui:operator"


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


def _is_operator(request: Request) -> bool:
    return getattr(request.state, "role", None) == "operator"


def _store(request: Request) -> CancelStore:
    store: CancelStore = request.app.state.cancel_store
    return store


async def _json_body(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:  # reason: malformed body is a client error, not a 500
        return None
    return body if isinstance(body, dict) else None


async def list_cancellations(request: Request) -> JSONResponse:
    """GET /api/cancellations — pending requests (visible to any authed role)."""
    pending = await _store(request).list(status="pending")
    return JSONResponse({"cancellations": [r.model_dump(mode="json") for r in pending]})


async def request_cancellation(request: Request) -> JSONResponse:
    """POST /api/cancellations — park a pending cancel request (operator only)."""
    if not _is_operator(request):
        emit_mutation_audit(
            request, target="run:cancel", operation="run.cancel", outcome="denied",
            detail="viewer role",
        )
        return _error("operator_role_required", 403)

    body = await _json_body(request)
    if body is None:
        return _error("expected a JSON object body", 400)

    # A request naming neither target can never match a live run — the model's
    # validator rejects it; surface that as a 400, not a 500.
    try:
        req = CancelRequest(
            id=uuid.uuid4().hex[:16],
            run_id=str(body.get("run_id") or ""),
            session_key=str(body.get("session_key") or ""),
            reason=str(body.get("reason") or ""),
            requested_by=_OPERATOR_DID,
        )
    except ValidationError:
        return _error("a run_id or session_key is required", 400)

    created = await _store(request).create(req)
    target = f"run:{created.run_id}" if created.run_id else f"session:{created.session_key}"
    emit_mutation_audit(request, target=target, operation="run.cancel", outcome="applied")
    return JSONResponse(created.model_dump(mode="json"), status_code=201)


routes = [
    Route("/api/cancellations", list_cancellations, methods=["GET"]),
    Route("/api/cancellations", request_cancellation, methods=["POST"]),
]

__all__ = ["list_cancellations", "request_cancellation", "routes"]
