"""Agents REST routes — list and detail.

GET  /api/agents              — List connected agents
GET  /api/agents/{id}         — Agent details
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.schemas import (
    AgentsListResponse,
    ErrorResponse,
)

logger = logging.getLogger(__name__)


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
                "workspace_path": r.workspace_path,
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
        return JSONResponse(meta)

    return JSONResponse(
        ErrorResponse(error="Agent not found").model_dump(mode="json"),
        status_code=404,
    )


routes = [
    Route("/api/agents", list_agents, methods=["GET"]),
    Route("/api/agents/{id}", get_agent, methods=["GET"]),
]
