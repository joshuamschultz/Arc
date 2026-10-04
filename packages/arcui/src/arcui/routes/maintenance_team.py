"""Settings -> Maintenance -> Team members: the arcteam registry from the browser.

The browser's twin of ``arc team entities`` and ``arc team register``. It reads and
writes the one :class:`arcteam.registry.EntityRegistry` the messaging layer uses, so
a person added here is addressable as ``@handle`` in team chat at once.

* **List** (any signed-in viewer): every registered agent and person, with status.
* **Add a person** (operator): a new human member. The identity is minted here and
  only its public half is kept; people never sign with a key of their own, because
  every dashboard message is sent under the operator's key.
* **Switch off / on / remove** (operator): ``suspended`` and ``active`` are
  reversible; ``revoked`` is the hard cut. The registry keeps the record, so the
  audit trail still names who the member was.
* The operator's own entry cannot be switched off: every dashboard message is sent
  as that entry, and losing it would silence the dashboard.

Re-registering an agent is not a route of its own: it is
``POST /api/agents/{id}/register``, which this tab calls.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from arcteam.types import Entity, EntityStatus, EntityType
from arctrust import AgentIdentity
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit, emit_read_audit
from arcui.routes.agent_detail.config_files import _error

logger = logging.getLogger("arcui.routes.maintenance_team")

_HANDLE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,39}$")
_ROLE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_MAX_ROLES = 8
_OPERATOR_HANDLE = "operator"

_OFFLINE = "Team messaging is offline right now, so the member list is not available."
_OPERATOR_PROTECTED = "The operator account cannot be switched off or removed here."


class _AddBody(BaseModel):
    """A person: a handle, a display name and optional roles. Nothing else is accepted."""

    model_config = ConfigDict(extra="forbid")

    handle: str
    name: str = Field(min_length=1, max_length=80)
    roles: list[str] = Field(default_factory=list, max_length=_MAX_ROLES)


class _StatusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    did: str
    status: EntityStatus


def _registry(request: Request) -> Any | None:
    return getattr(request.app.state, "messaging_registry", None)


def _is_protected(entity: Entity) -> bool:
    return entity.type == EntityType.USER and entity.handle == _OPERATOR_HANDLE


def _member(entity: Entity) -> dict[str, Any]:
    return {
        "did": entity.did,
        "handle": entity.handle,
        "name": entity.name,
        "type": entity.type.value,
        "roles": list(entity.roles),
        "status": entity.status.value,
        "harness": entity.harness,
        "created": entity.created,
        "protected": _is_protected(entity),
    }


async def get_members(request: Request) -> JSONResponse:
    """GET /api/maintenance/team/members — every registered agent and person."""
    registry = _registry(request)
    if registry is None:
        return _error(_OFFLINE, 503)
    try:
        entities = await registry.list_entities()
    except Exception:  # reason: an unreachable broker is "offline", never an empty team
        logger.warning("team members: list failed", exc_info=True)
        return _error(_OFFLINE, 503)
    emit_read_audit(request, target="team", operation="team.member.list", outcome="ok")
    members = sorted((_member(e) for e in entities), key=lambda m: (m["type"], m["handle"]))
    return JSONResponse({"members": members})


def _gate(request: Request, operation: str) -> JSONResponse | None:
    if getattr(request.state, "role", None) == "operator":
        return None
    emit_mutation_audit(
        request, target="team", operation=operation, outcome="denied", detail="not an operator"
    )
    return _error("Only an operator can change the team.", 403)


def _refuse(
    request: Request, operation: str, target: str, message: str, status: int
) -> JSONResponse:
    emit_mutation_audit(
        request, target=target, operation=operation, outcome="denied", detail=message[:120]
    )
    return _error(message, status)


async def _body(request: Request, model: type[BaseModel]) -> Any | None:
    try:
        return model.model_validate(await request.json())
    except (ValueError, ValidationError):
        return None


def _new_person(body: _AddBody) -> Entity:
    """A person's registry entry. Only the public half of the minted identity is kept."""
    identity = AgentIdentity.generate(org="local", agent_type="user")
    return Entity(
        did=identity.did,
        handle=body.handle,
        id=f"user://{body.handle}",
        name=body.name.strip(),
        type=EntityType.USER,
        public_key=identity.public_key.hex(),
        roles=body.roles,
    )


async def add_member(request: Request) -> JSONResponse:
    """POST /api/maintenance/team/members — add a person. Operator only."""
    operation = "team.member.add"
    refused = _gate(request, operation)
    if refused is not None:
        return refused
    body = await _body(request, _AddBody)
    if body is None or not _HANDLE.fullmatch(body.handle) or not body.name.strip():
        return _refuse(
            request,
            operation,
            "team",
            "Use a short lowercase handle (letters, digits, - or _) and a name.",
            400,
        )
    if not all(_ROLE.fullmatch(role) for role in body.roles):
        return _refuse(request, operation, "team", "Roles are short lowercase words.", 400)
    registry = _registry(request)
    if registry is None:
        return _error(_OFFLINE, 503)
    target = f"member:{body.handle}"
    try:
        entity = _new_person(body)
        await registry.register(entity)
    except ValueError:
        return _refuse(
            request, operation, target, f"The handle {body.handle} is already taken.", 409
        )
    emit_mutation_audit(
        request, target=target, operation=operation, outcome="applied", detail=f"did={entity.did}"
    )
    return JSONResponse(_member(entity), status_code=201)


async def set_member_status(request: Request) -> JSONResponse:
    """POST /api/maintenance/team/members/status — switch off, on, or remove. Operator only."""
    operation = "team.member.status"
    refused = _gate(request, operation)
    if refused is not None:
        return refused
    body = await _body(request, _StatusBody)
    if body is None or not body.did.startswith("did:"):
        return _refuse(request, operation, "team", "Choose a member and a status.", 400)
    registry = _registry(request)
    if registry is None:
        return _error(_OFFLINE, 503)
    target = f"member:{body.did}"
    entity = await registry.get(body.did)
    if entity is None:
        return _refuse(request, operation, target, "That member is not on the team.", 404)
    if _is_protected(entity):
        return _refuse(request, operation, target, _OPERATOR_PROTECTED, 409)
    entity.status = body.status
    await registry.update(entity)
    emit_mutation_audit(
        request, target=target, operation=operation, outcome="applied", detail=body.status.value
    )
    return JSONResponse(_member(entity))


routes = [
    Route("/api/maintenance/team/members", get_members, methods=["GET"]),
    Route("/api/maintenance/team/members", add_member, methods=["POST"]),
    Route("/api/maintenance/team/members/status", set_member_status, methods=["POST"]),
]

__all__ = ["add_member", "get_members", "routes", "set_member_status"]
