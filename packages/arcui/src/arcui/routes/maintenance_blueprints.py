"""Settings -> Maintenance -> Blueprints: list presets and build an agent from one.

The browser's twin of ``arc blueprint list`` and ``arc init --blueprint``. A
blueprint is a signed preset folder; building an agent from one runs the same two
steps the terminal runs, with nothing re-implemented here:

1. :func:`arcagent.scaffold.create_agent` makes the agent (DID, config, workspace);
2. :func:`arcagent.blueprints_materialize.materialize_blueprint` writes the
   blueprint's persona, operator-signed prompt overlays, operator-signed skills and
   capabilities, and seeded schedules.

A blueprint is chosen from the list by its folder name; the name is matched against
what the loader lists, so a path or ``..`` can never reach the resolver. If step 2
fails the half-built agent is removed.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path
from typing import Any

import arcagent
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui import prompt_signing
from arcui.audit import emit_mutation_audit, emit_read_audit
from arcui.routes.agent_detail.config_files import _error
from arcui.routes.agents import agent_audit, operator_signing, register_created_agent
from arcui.routes.trust import operator_signer_for_request

logger = logging.getLogger("arcui.routes.maintenance_blueprints")

_BLUEPRINT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

_NOT_FOUND = "That blueprint is not on the list."
_UNSIGNED = (
    "This blueprint is not valid, or it is not signed by this Arc's operator key, "
    "so it was not used."
)
_BROKEN = "This blueprint could not be applied, so no agent was made."
_NO_KEY = "Arc's operator key is unavailable, so a new agent cannot be signed."


class _CreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_name: str
    model: str = arcagent.scaffold.DEFAULT_MODEL
    tier: str = "personal"


def _operator_key(request: Request) -> bytes | None:
    """The operator public key signed presets are pinned to, or ``None`` when unreadable."""
    try:
        key: bytes = operator_signer_for_request(request).public_key
    except Exception:  # reason: an unreadable key only means user presets show as unsigned
        logger.warning("blueprints: operator key unreadable", exc_info=True)
        return None
    return key


def _creates(blueprint: arcagent.blueprints.ResolvedBlueprint) -> dict[str, Any]:
    """What building an agent from this blueprint would put on disk."""
    root = blueprint.root
    prompts = (
        sorted(f"{p.parent.name}/{p.stem}" for p in root.glob("prompts/*/*.md")) if root else []
    )
    skills = (
        sorted(p.parent.name for p in blueprint.skills_dir.glob("*/SKILL.md"))
        if blueprint.skills_dir
        else []
    )
    capabilities = (
        sorted(p.name for p in blueprint.capabilities_dir.glob("*.py"))
        if blueprint.capabilities_dir
        else []
    )
    modules_table = blueprint.overlay.get("modules", {})
    modules = sorted(
        name
        for name, entry in modules_table.items()
        if isinstance(entry, dict) and entry.get("enabled")
    )
    return {
        "persona": blueprint.persona is not None,
        "prompts": prompts,
        "skills": skills,
        "capabilities": capabilities,
        "schedules": len(blueprint.schedules),
        "modules": modules,
    }


def _summary(blueprint: arcagent.blueprints.ResolvedBlueprint) -> dict[str, Any]:
    folder = blueprint.root.name if blueprint.root else blueprint.name
    return {
        "id": folder,
        "name": blueprint.name,
        "version": blueprint.version,
        "tier": blueprint.tier,
        "description": blueprint.description,
        "source": blueprint.source,
        "signed": blueprint.source == "packaged" or blueprint.signed,
        "creates": _creates(blueprint),
    }


def _listed(request: Request) -> list[arcagent.blueprints.ResolvedBlueprint]:
    return arcagent.blueprints.list_blueprints(operator_public_key=_operator_key(request))


async def get_blueprints(request: Request) -> JSONResponse:
    """GET /api/maintenance/blueprints — every preset and what it would create."""
    rows = await asyncio.to_thread(lambda: [_summary(b) for b in _listed(request)])
    emit_read_audit(request, target="blueprints", operation="blueprint.list", outcome="ok")
    return JSONResponse({"blueprints": rows})


# ----------------------------------------------------------------------------- create


def _refuse(request: Request, target: str, detail: str, message: str, status: int) -> JSONResponse:
    agent_audit(request, target, "blueprint.create", "denied", detail)
    return _error(message, status)


def _materialize(
    request: Request, blueprint: arcagent.blueprints.ResolvedBlueprint, agent_dir: Path, tier: str
) -> arcagent.blueprints_materialize.MaterializeResult:
    identity = prompt_signing.signer_for(request)
    signer = operator_signer_for_request(request)
    return arcagent.blueprints_materialize.materialize_blueprint(
        blueprint,
        agent_dir,
        deployment_tier=tier,
        operator_signer=(identity.did, identity.seed),
        capability_signer=arcagent.blueprints_materialize.CapabilitySigner(
            did=identity.did, signer=signer
        ),
    )


def _created_summary(result: arcagent.blueprints_materialize.MaterializeResult) -> dict[str, Any]:
    return {
        "persona": result.wrote_identity,
        "prompts": len(result.prompt_overlays),
        "capabilities": len(result.capabilities),
        "skills": len(result.skills),
        "schedules": result.schedules,
    }


async def create_from_blueprint(request: Request) -> JSONResponse:
    """POST /api/maintenance/blueprints/{blueprint}/create — build an agent. Operator only."""
    blueprint_id = request.path_params["blueprint"]
    if getattr(request.state, "role", None) != "operator":
        return _refuse(
            request, "agent:new", "not an operator", "Only an operator can add agents.", 403
        )

    chosen = _chosen(request, blueprint_id)
    if chosen is None:
        return _refuse(request, "agent:new", "unknown blueprint", _NOT_FOUND, 404)
    try:
        body = _CreateBody.model_validate(await request.json())
    except (ValueError, ValidationError):
        return _error("Enter a name for the new agent.", 400)
    try:
        name = arcagent.scaffold.validate_agent_name(body.agent_name)
    except arcagent.scaffold.AgentNameError as exc:
        return _refuse(request, "agent:invalid-name", "unsafe name", str(exc), 400)

    team_root: Path | None = getattr(request.app.state, "team_root", None)
    if team_root is None:
        return _error("This Arc has no fleet folder, so it cannot hold agents.", 503)
    target = f"agent:{name}"
    tier = str(arcagent.stricter_tier(str(arcagent.deployment_tier()), body.tier))
    try:
        operator = operator_signing(request)
        blueprint = await asyncio.to_thread(
            arcagent.blueprints.resolve_blueprint,
            chosen,
            tier=tier,
            operator_public_key=operator.signer.public_key,
        )
    except (FileNotFoundError, ValueError) as exc:
        logger.info("blueprints: %s refused: %s", chosen, exc)
        return _refuse(request, target, "blueprint refused", _UNSIGNED, 409)
    except Exception:  # reason: no signer means no signed identity; refuse before writing
        logger.warning("blueprints: operator signer unavailable", exc_info=True)
        return _refuse(request, target, "operator signer unavailable", _NO_KEY, 503)

    try:
        created = await asyncio.to_thread(
            arcagent.scaffold.create_agent,
            team_root,
            name,
            tier=body.tier,
            model=body.model,
            operator=operator,
        )
    except arcagent.scaffold.AgentExistsError as exc:
        return _refuse(request, target, "already exists", str(exc), 409)
    except ValueError as exc:
        return _refuse(request, target, "invalid input", str(exc), 400)

    try:
        result = await asyncio.to_thread(_materialize, request, blueprint, created.agent_dir, tier)
    except Exception:  # reason: never leave a half-built agent the roster would list
        logger.warning("blueprints: materialize failed for %s", chosen, exc_info=True)
        shutil.rmtree(created.agent_dir, ignore_errors=True)
        return _refuse(request, target, "materialize failed", _BROKEN, 409)

    notice = await register_created_agent(request, created)
    emit_mutation_audit(
        request,
        target=target,
        operation="blueprint.create",
        outcome="applied",
        detail=f"blueprint={chosen} did={created.did}",
    )
    return JSONResponse(
        {
            "agent_id": created.name,
            "did": created.did,
            "team_registered": notice is None,
            "notice": notice,
            "created": _created_summary(result),
            "warnings": len(result.unsigned_warnings),
        },
        status_code=201,
    )


def _chosen(request: Request, blueprint_id: str) -> str | None:
    """The listed folder name for ``blueprint_id``, or ``None`` when it is not on the list."""
    if not _BLUEPRINT_ID.fullmatch(blueprint_id):
        return None
    listed = {b.root.name for b in _listed(request) if b.root is not None}
    return blueprint_id if blueprint_id in listed else None


routes = [
    Route("/api/maintenance/blueprints", get_blueprints, methods=["GET"]),
    Route(
        "/api/maintenance/blueprints/{blueprint}/create", create_from_blueprint, methods=["POST"]
    ),
]

__all__ = ["create_from_blueprint", "get_blueprints", "routes"]
