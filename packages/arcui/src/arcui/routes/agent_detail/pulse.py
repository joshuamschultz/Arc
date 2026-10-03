"""Pulse approval — ``GET/POST /api/agents/{id}/pulse[/approve]``.

A pulse check runs only from an operator-approved revision. ``GET`` lists every
check in the agent's ``pulse.md`` with its review state and a diff against the
last approved definition; ``POST .../approve`` approves one check exactly as the
operator reviewed it: the request names the definition digest the operator saw,
and the approval is refused if the text changed since, or if it is already
approved (a replay). The signed revision comes from the control authority
(``register_revision(purpose="pulse")``); this route never signs anything itself.
Every mutation outcome is audited.

The same module is the ONE operator-only writer of ``pulse.md``: ``POST .../pulse``
adds a check, ``PUT``/``DELETE .../pulse/{name}`` edit or remove one (naming the
digest the operator saw), and ``DELETE .../pulse/proposals/{name}`` dismisses an
agent proposal. A write never approves: the check reads "pending approval" and goes
through the approve flow above. Writes are validated against the pulse.md schema,
atomic, and audited; viewers and agents are refused before anything is read.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
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
    workspace = agent_root / "workspace"
    checks = [status.to_wire() for status in arcagent.pulse_status(workspace)]
    available = getattr(request.app.state, "schedule_control_authority", None) is not None
    return JSONResponse(
        {
            "checks": checks,
            "proposals": arcagent.list_pulse_proposals(workspace),
            "authority_available": available,
        }
    )


def _audit(
    request: Request, target: str, outcome: str, detail: str, operation: str = _OPERATION
) -> None:
    emit_mutation_audit(
        request, target=target, operation=operation, outcome=outcome, detail=detail
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


async def _write_gate(
    request: Request, operation: str, name: str | None
) -> tuple[Path | None, dict[str, Any], JSONResponse | None]:
    """Operator role, agent, JSON body: (workspace, body, refusal). Role first, then audit."""
    agent_id = request.path_params["id"]
    target = f"pulse:{name}" if name else f"pulse:{agent_id}"
    if getattr(request.state, "role", None) != "operator":
        _audit(request, target, "denied", "viewer role", operation)
        return None, {}, _error("operator_role_required", 403)
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return None, {}, _error("Agent not found", 404)
    parsed = await _json_body(request)
    if parsed is None and request.method != "DELETE":
        return None, {}, _error("expected a JSON object", 400)
    return agent_root / "workspace", parsed or {}, None


def _fields(body: dict[str, Any]) -> tuple[int, str] | None:
    interval, action = body.get("interval_minutes"), body.get("action")
    if not isinstance(interval, int) or isinstance(interval, bool) or not isinstance(action, str):
        return None
    return interval, action


def _digest_of(workspace: Path, name: str) -> str | None:
    found = next((s for s in arcagent.pulse_status(workspace) if s.name == name), None)
    return None if found is None else found.definition_digest


async def post_pulse_add(request: Request) -> JSONResponse:
    """POST /api/agents/{id}/pulse — add one unapproved check (operator only)."""
    probe = await _json_body(request)
    name = probe.get("name") if probe else None
    name = name if isinstance(name, str) else None
    workspace, body, refusal = await _write_gate(request, "pulse.add", name)
    if refusal is not None or workspace is None:
        return refusal or _error("Agent not found", 404)
    fields = _fields(body)
    if not isinstance(name, str) or fields is None:
        return _error("expected {name, interval_minutes, action}", 400)
    target = f"pulse:{name}"
    try:
        arcagent.add_pulse_check(
            workspace, name=name, interval_minutes=fields[0], action=fields[1]
        )
    except arcagent.PulseCheckInvalidError as exc:
        duplicate = "already exists" in str(exc)
        _audit(request, target, "denied", str(exc), "pulse.add")
        return _error(str(exc), 409 if duplicate else 400)
    except OSError as exc:
        _audit(request, target, "error", str(exc), "pulse.add")
        return _error("pulse.md could not be written", 503)
    proposal = body.get("proposal")
    if isinstance(proposal, str) and proposal:
        arcagent.clear_pulse_proposal(workspace, proposal)
    _audit(request, target, "applied", "added; pending approval", "pulse.add")
    return JSONResponse({"check": name, "status": "unapproved"}, status_code=201)


async def _reviewed_check(
    request: Request, operation: str
) -> tuple[Path, str, dict[str, Any]] | JSONResponse:
    """Shared edit/remove gate: operator, existing check, and the digest the operator saw."""
    name = request.path_params["name"]
    workspace, body, refusal = await _write_gate(request, operation, name)
    if refusal is not None or workspace is None:
        return refusal or _error("Agent not found", 404)
    target = f"pulse:{name}"
    current = _digest_of(workspace, name)
    if current is None:
        return _error("not found", 404)
    seen = body.get("definition_digest")
    if not isinstance(seen, str) or not seen:
        return _error("expected definition_digest", 400)
    if seen != current:
        _audit(request, target, "denied", "changed since reviewed", operation)
        return _error("pulse check changed since it was reviewed", 409)
    return workspace, name, body


async def put_pulse_check(request: Request) -> JSONResponse:
    """PUT /api/agents/{id}/pulse/{name} — rewrite one check; it returns to pending approval."""
    gate = await _reviewed_check(request, "pulse.edit")
    if isinstance(gate, JSONResponse):
        return gate
    workspace, name, body = gate
    fields = _fields(body)
    if fields is None:
        return _error("expected {interval_minutes, action, definition_digest}", 400)
    try:
        arcagent.edit_pulse_check(workspace, name, interval_minutes=fields[0], action=fields[1])
    except arcagent.PulseCheckInvalidError as exc:
        _audit(request, f"pulse:{name}", "denied", str(exc), "pulse.edit")
        return _error(str(exc), 400)
    except OSError as exc:
        _audit(request, f"pulse:{name}", "error", str(exc), "pulse.edit")
        return _error("pulse.md could not be written", 503)
    _audit(request, f"pulse:{name}", "applied", "edited; pending approval", "pulse.edit")
    return JSONResponse({"check": name, "status": "changes_pending"})


async def delete_pulse_check(request: Request) -> JSONResponse:
    """DELETE /api/agents/{id}/pulse/{name} — remove one check."""
    gate = await _reviewed_check(request, "pulse.remove")
    if isinstance(gate, JSONResponse):
        return gate
    workspace, name, _ = gate
    try:
        arcagent.remove_pulse_check(workspace, name)
    except arcagent.PulseCheckInvalidError as exc:
        return _error(str(exc), 404)
    except OSError as exc:
        _audit(request, f"pulse:{name}", "error", str(exc), "pulse.remove")
        return _error("pulse.md could not be written", 503)
    _audit(request, f"pulse:{name}", "applied", "removed", "pulse.remove")
    return JSONResponse({"check": name, "removed": True})


async def delete_pulse_proposal(request: Request) -> JSONResponse:
    """DELETE /api/agents/{id}/pulse/proposals/{name} — dismiss an agent proposal."""
    name = request.path_params["name"]
    workspace, _, refusal = await _write_gate(request, "pulse.dismiss", name)
    if refusal is not None or workspace is None:
        return refusal or _error("Agent not found", 404)
    arcagent.clear_pulse_proposal(workspace, name)
    _audit(request, f"pulse:{name}", "applied", "proposal dismissed", "pulse.dismiss")
    return JSONResponse({"proposal": name, "dismissed": True})


__all__ = [
    "delete_pulse_check",
    "delete_pulse_proposal",
    "get_pulse",
    "post_pulse_add",
    "post_pulse_approve",
    "put_pulse_check",
]
