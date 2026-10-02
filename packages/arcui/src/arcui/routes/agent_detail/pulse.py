"""Pulse approval — ``GET/POST /api/agents/{id}/pulse[/approve]``.

A pulse check runs only from an operator-approved revision. ``GET`` lists every
check in the agent's ``pulse.md`` with its review state and a diff against the
last approved definition; ``POST .../approve`` approves one check exactly as the
operator reviewed it: the request names the definition digest the operator saw,
and the approval is refused if the text changed since, or if it is already
approved (a replay). The signed revision comes from the control authority
(``register_revision(purpose="pulse")``); this route never signs anything itself.
Every mutation outcome is audited.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

import arcagent
from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.audit import emit_mutation_audit
from arcui.routes.agent_detail._common import _agent_did, _agent_root
from arcui.schemas import ErrorResponse

_OPERATION = "pulse.approve"
#: Refusals that mean "the operator's view is out of date", not "not permitted".
_STALE_MARKERS = ("changed since", "stale", "ambiguous")


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


async def _json_body(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:  # reason: a malformed body is a client error, not a 500
        return None
    return body if isinstance(body, dict) else None


async def get_pulse(request: Request) -> JSONResponse:
    """GET /api/agents/{id}/pulse — checks, approval state and diffs."""
    agent_root = _agent_root(request, request.path_params["id"])
    if agent_root is None:
        return _error("Agent not found", 404)
    checks = [status.to_wire() for status in arcagent.pulse_status(agent_root / "workspace")]
    available = getattr(request.app.state, "schedule_control_authority", None) is not None
    return JSONResponse({"checks": checks, "authority_available": available})


def _audit(request: Request, target: str, outcome: str, detail: str) -> None:
    emit_mutation_audit(
        request, target=target, operation=_OPERATION, outcome=outcome, detail=detail
    )


async def post_pulse_approve(request: Request) -> JSONResponse:
    """POST /api/agents/{id}/pulse/approve — approve one reviewed check (operator only)."""
    agent_id = request.path_params["id"]
    body = await _json_body(request)
    name = None if body is None else body.get("check")
    target = f"pulse:{name}" if isinstance(name, str) else f"pulse:{agent_id}"

    if getattr(request.state, "role", None) != "operator":
        _audit(request, target, "denied", "viewer role")
        return _error("operator_role_required", 403)
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return _error("Agent not found", 404)
    digest = None if body is None else body.get("definition_digest")
    if not isinstance(name, str) or not isinstance(digest, str) or not name or not digest:
        return _error("expected {check, definition_digest}", 400)

    workspace = agent_root / "workspace"
    current = next((s for s in arcagent.pulse_status(workspace) if s.name == name), None)
    if current is None:
        return _error("not found", 404)
    if current.approved:
        _audit(request, target, "denied", "already approved")
        return _error("pulse check is already approved", 409)
    if current.definition_digest != digest:
        _audit(request, target, "denied", "changed since reviewed")
        return _error("pulse check changed since it was reviewed", 409)
    return await _approve(request, workspace, name, digest, target, agent_id)


async def _approve(
    request: Request, workspace: Any, name: str, digest: str, target: str, agent_id: str
) -> JSONResponse:
    authority = getattr(request.app.state, "schedule_control_authority", None)
    tenant_id = getattr(request.app.state, "schedule_tenant_id", None)
    proof_issuer = cast(
        Callable[[Request, str, str, bytes], Awaitable[bytes]] | None,
        getattr(request.app.state, "schedule_operator_proof_issuer", None),
    )
    agent_did = _agent_did(request, agent_id)
    if authority is None or tenant_id is None or proof_issuer is None or agent_did is None:
        _audit(request, target, "error", "signed pulse authority unavailable")
        return _error("signed pulse authority unavailable", 503)

    async def actor_proof_source(purpose: str, artifact_id: str, definition: bytes) -> bytes:
        return await proof_issuer(request, purpose, artifact_id, definition)

    try:
        approval = await arcagent.approve_pulse_check(
            workspace,
            name,
            reviewed_digest=digest,
            tenant_id=tenant_id,
            agent_did=agent_did,
            authority=authority,
            actor_proof_source=actor_proof_source,
        )
    except arcagent.ControlArtifactRefusedError as exc:
        _audit(request, target, "denied", str(exc))
        stale = any(marker in str(exc) for marker in _STALE_MARKERS)
        return _error(str(exc), 409 if stale else 403)
    except (arcagent.ControlArtifactUnavailableError, OSError) as exc:
        _audit(request, target, "error", str(exc))
        return _error("signed pulse authority unavailable", 503)
    _audit(request, target, "applied", f"revision {approval.revision}")
    return JSONResponse({"check": name, "revision": approval.revision, "approved": True})


__all__ = ["get_pulse", "post_pulse_approve"]
