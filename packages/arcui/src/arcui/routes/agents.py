"""Agents REST routes — list, detail, and making agents from the browser (J-O2).

GET  /api/agents                  — List connected agents
GET  /api/agents/{id}             — Agent details (+ ``team_registered``)
POST /api/agents                  — Create a new agent (operator only)
POST /api/agents/import           — Create one from imported persona files (operator only)
POST /api/agents/{id}/register    — Add an agent to the team registry (operator only)

Create and import go through :func:`arcagent.scaffold.create_agent`, the same
function ``arc agent create`` uses: three config files, a minted DID, and
``identity.md`` + the example capability signed through the operator signer
handle. Registration is :func:`arcteam.registry.register_native_agent`, the same
shape ``arc agent create`` registers with. Every attempt is audited.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Literal

import arcagent
from arcteam.registry import register_native_agent
from arctrust.policy import OperatorApprovalAuthority
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit
from arcui.auth import ui_session_actor
from arcui.identity import resolve_agent_identity
from arcui.routes.trust import operator_signer_for_request
from arcui.schemas import (
    AgentsListResponse,
    ErrorResponse,
)

logger = logging.getLogger(__name__)

_MESSAGING_OFFLINE = (
    "Team messaging is offline, so this agent is not in team chat yet. "
    "Open the agent and choose Add to team once messaging is back."
)
_REGISTER_FAILED = (
    "The agent was created, but adding it to team chat failed. "
    "Open the agent and choose Add to team to try again."
)


async def list_agents(request: Request) -> JSONResponse:
    """GET /api/agents — List all connected agents."""
    registry = request.app.state.agent_registry
    agents = [a.model_dump() for a in registry.list_agents()]
    return JSONResponse(AgentsListResponse(agents=agents).model_dump(mode="json"))


async def get_agent(request: Request) -> JSONResponse:
    """GET /api/agents/{id} — Agent details.

    Single source of truth: the disk roster (`arcagent.toml`) merged with
    the live WS registry overlay. Same shape `/api/team/roster` returns —
    just filtered to one row plus WS-only fields (tools, modules,
    connected_at, sequence) when the agent is currently connected.

    Resolution order:
      1. Roster (when team_root is configured) — preferred. Carries did,
         display_name, role_label, color, org, type from arcagent.toml.
      2. Registry-only — for deployments / tests with no team_root. Returns
         the live registration as-is with online=True.
      3. 404 if neither has the agent.
    """
    agent_id = request.path_params["id"]
    registry = request.app.state.agent_registry
    roster_provider = getattr(request.app.state, "roster_provider", None)

    if roster_provider is not None:
        for r in roster_provider():
            if r.agent_id != agent_id:
                continue
            data = {
                "agent_id": r.agent_id,
                "name": r.name,
                "did": r.did,
                "org": r.org,
                "type": r.type,
                "model": r.model,
                "provider": r.provider,
                "online": r.online,
                "display_name": r.display_name,
                "color": r.color,
                "role_label": r.role_label,
                "hidden": r.hidden,
                "harness": getattr(r, "harness", "arcagent"),
                "workspace_path": r.workspace_path,
                # H-007: canonical identity — DID parsed + roster-name joined
                # by DID, so the header renders the same shape every list of
                # agents does (see arcui.identity, routes/team_pages.py).
                "identity": resolve_agent_identity(r.did, r.display_name or r.name).model_dump(),
                "team_registered": await _team_registered(request, r.did),
            }
            if r.online:
                entry = registry.get(agent_id)
                if entry is not None:
                    live = entry.registration
                    data.update(
                        {
                            "agent_name": live.agent_name,
                            "tools": live.tools,
                            "modules": live.modules,
                            "workspace": live.workspace,
                            "team": live.team,
                            "meta": live.meta,
                            "connected_at": live.connected_at,
                            "last_event_at": live.last_event_at,
                            "sequence": live.sequence,
                        }
                    )
            return JSONResponse(data)

    # No roster (or roster has no row for this id) — fall back to the
    # in-memory registry. Used by no-team_root deployments and tests.
    entry = registry.get(agent_id)
    if entry is not None:
        meta = entry.registration.model_dump()
        meta.setdefault("agent_id", agent_id)
        meta["online"] = True
        # No roster row to carry a `did` — a registration's own `meta` is the
        # only place one could have been reported. Still resolve so the
        # response always carries the canonical shape (safe "unknown" parts
        # when there is no DID at all, per arcui.identity.parse_did).
        reported_did = entry.registration.meta.get("did", "")
        meta["identity"] = resolve_agent_identity(
            reported_did if isinstance(reported_did, str) else "",
            entry.registration.agent_name,
        ).model_dump()
        meta["team_registered"] = None
        return JSONResponse(meta)

    return JSONResponse(
        ErrorResponse(error="Agent not found").model_dump(mode="json"),
        status_code=404,
    )


# --- making agents from the browser ------------------------------------------


class _Strict(BaseModel):
    """Unknown fields are refused, so a body cannot pick a directory or a key."""

    model_config = ConfigDict(extra="forbid")


class CreateAgentBody(_Strict):
    name: str
    model: str = arcagent.scaffold.DEFAULT_MODEL
    tier: Literal["personal", "enterprise", "federal"] = "personal"


class ImportAgentBody(CreateAgentBody):
    files: dict[str, str]


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=message).model_dump(mode="json"), status_code=status)


def _audit(request: Request, target: str, operation: str, outcome: str, detail: str = "") -> None:
    emit_mutation_audit(
        request,
        target=target,
        operation=operation,
        outcome=outcome,
        detail=f"actor={ui_session_actor(request)} {detail}".strip(),
    )


def _operator_only(request: Request, target: str, operation: str) -> JSONResponse | None:
    if getattr(request.state, "role", None) == "operator":
        return None
    _audit(request, target, operation, "denied", "not an operator")
    return _error("Only an operator can add agents.", 403)


async def _team_registered(request: Request, did: str) -> bool | None:
    """Is ``did`` in the team registry? None when messaging is offline (unknown)."""
    registry = getattr(request.app.state, "messaging_registry", None)
    if registry is None or not did:
        return None
    try:
        return await registry.get(did) is not None
    except Exception:  # reason: an unreachable broker means "unknown", not "no"
        logger.warning("agents.team_registered lookup failed", exc_info=True)
        return None


async def _register(request: Request, created: arcagent.scaffold.CreatedAgent) -> str | None:
    """Register ``created`` with the team; return a plain notice when it could not be."""
    registry = getattr(request.app.state, "messaging_registry", None)
    if registry is None:
        return _MESSAGING_OFFLINE
    try:
        await register_native_agent(
            registry,
            name=created.name,
            did=created.did,
            public_key_hex=created.public_key_hex,
            workspace_path=str(created.agent_dir / "workspace"),
        )
    except Exception:  # reason: the agent exists; registration is retryable from its page
        logger.warning("agents.register failed name=%s", created.name, exc_info=True)
        return _REGISTER_FAILED
    return None


async def _parse(request: Request, model: type[CreateAgentBody]) -> CreateAgentBody | str:
    try:
        raw = await request.json()
    except Exception:  # reason: a malformed body is a bad request, not a 500
        return "Expected a JSON body."
    try:
        return model.model_validate(raw)
    except ValidationError:
        return "Enter an agent name. Only the name, model, tier and files can be sent."


def _operator(request: Request) -> arcagent.scaffold.OperatorSigning:
    signer = operator_signer_for_request(request)
    return arcagent.scaffold.OperatorSigning(
        did=OperatorApprovalAuthority(signer).did, signer=signer
    )


async def _create(request: Request, model: type[CreateAgentBody], operation: str) -> JSONResponse:
    refused = _operator_only(request, "agent:new", operation)
    if refused is not None:
        return refused
    body = await _parse(request, model)
    if isinstance(body, str):
        return _error(body, 400)
    try:
        name = arcagent.scaffold.validate_agent_name(body.name)
    except arcagent.scaffold.AgentNameError as exc:
        _audit(request, "agent:invalid-name", operation, "denied", "unsafe name")
        return _error(str(exc), 400)
    target = f"agent:{name}"
    team_root: Path | None = getattr(request.app.state, "team_root", None)
    if team_root is None:
        return _error("This Arc has no fleet folder, so it cannot hold agents.", 503)
    try:
        operator = _operator(request)
    except Exception:  # reason: no signer means no signed identity; refuse before writing
        logger.warning("agents.create operator signer unavailable", exc_info=True)
        _audit(request, target, operation, "error", "operator signer unavailable")
        return _error("Arc's operator key is unavailable, so a new agent cannot be signed.", 503)
    documents = body.files if isinstance(body, ImportAgentBody) else None
    if documents is not None and "identity.md" not in documents:
        return _error("Choose the agent's identity.md file to import.", 400)
    try:
        created = await asyncio.to_thread(
            arcagent.scaffold.create_agent,
            team_root,
            name,
            tier=body.tier,
            model=body.model,
            operator=operator,
            documents=documents,
        )
    except arcagent.scaffold.AgentExistsError as exc:
        _audit(request, target, operation, "denied", "already exists")
        return _error(str(exc), 409)
    except ValueError as exc:
        _audit(request, target, operation, "denied", "invalid input")
        return _error(str(exc), 400)
    notice = await _register(request, created)
    _audit(request, target, operation, "applied", f"did={created.did}")
    return JSONResponse(
        {
            "agent_id": created.name,
            "name": created.name,
            "did": created.did,
            "team_registered": notice is None,
            "notice": notice,
        },
        status_code=201,
    )


async def create_agent(request: Request) -> JSONResponse:
    """POST /api/agents — a new agent, built exactly as ``arc agent create`` builds one."""
    return await _create(request, CreateAgentBody, "agent.create")


async def import_agent(request: Request) -> JSONResponse:
    """POST /api/agents/import — a new agent whose persona files come from elsewhere.

    Only persona text travels (identity.md, policy.md, context.md, pulse.md). Code
    and keys never do: the new agent gets its own DID, and the operator's signature
    on the imported identity.md is the approval of what it says.
    """
    return await _create(request, ImportAgentBody, "agent.import")


def _roster_entry(request: Request, agent_id: str) -> Any | None:
    provider = getattr(request.app.state, "roster_provider", None)
    if provider is None:
        return None
    return next((r for r in provider() if r.agent_id == agent_id), None)


async def register_agent(request: Request) -> JSONResponse:
    """POST /api/agents/{id}/register — add an existing agent to the team registry."""
    agent_id = request.path_params["id"]
    target = f"agent:{agent_id}"
    refused = _operator_only(request, target, "agent.register")
    if refused is not None:
        return refused
    entry = _roster_entry(request, agent_id)
    if entry is None or entry.harness != "arcagent":
        return _error("Agent not found", 404)
    registry = getattr(request.app.state, "messaging_registry", None)
    if registry is None:
        return _error("Team messaging is offline right now. Try again in a minute.", 503)
    agent_dir = Path(entry.workspace_path)
    try:
        identity = await asyncio.to_thread(arcagent.scaffold.mint_agent_identity, agent_dir)
        await register_native_agent(
            registry,
            name=agent_id,
            did=identity.did,
            public_key_hex=identity.public_key.hex(),
            workspace_path=str(agent_dir / "workspace"),
        )
    except ValueError as exc:
        _audit(request, target, "agent.register", "denied", str(exc))
        return _error(f"Could not add {agent_id} to the team: {exc}", 409)
    _audit(request, target, "agent.register", "applied", f"did={identity.did}")
    return JSONResponse({"agent_id": agent_id, "did": identity.did, "team_registered": True})


routes = [
    Route("/api/agents", list_agents, methods=["GET"]),
    Route("/api/agents", create_agent, methods=["POST"]),
    Route("/api/agents/import", import_agent, methods=["POST"]),
    Route("/api/agents/{id}", get_agent, methods=["GET"]),
    Route("/api/agents/{id}/register", register_agent, methods=["POST"]),
]
