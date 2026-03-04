"""Agents REST routes — list, detail, and control proxy.

GET  /api/agents              — List connected agents
GET  /api/agents/{id}         — Agent details
POST /api/agents/{id}/control — Send control command (operator only)
"""

from __future__ import annotations

import asyncio
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.types import ControlMessage, ControlResponse

logger = logging.getLogger(__name__)

_CONTROL_TIMEOUT_SECONDS = 30.0


async def list_agents(request: Request) -> JSONResponse:
    """GET /api/agents — List all connected agents."""
    registry = request.app.state.agent_registry
    agents = [a.model_dump() for a in registry.list_agents()]
    return JSONResponse({"agents": agents})


async def get_agent(request: Request) -> JSONResponse:
    """GET /api/agents/{id} — Agent details."""
    agent_id = request.path_params["id"]
    registry = request.app.state.agent_registry
    entry = registry.get(agent_id)
    if entry is None:
        return JSONResponse({"error": "Agent not found"}, status_code=404)
    return JSONResponse({"agent": entry.registration.model_dump()})


async def control_agent(request: Request) -> JSONResponse:
    """POST /api/agents/{id}/control — Send control command.

    Requires operator role. Sends ControlMessage to agent's WebSocket
    and waits for ControlResponse with timeout.
    """
    # Operator role check
    role = getattr(request.state, "role", None)
    if role != "operator":
        return JSONResponse(
            {"error": "Operator role required"}, status_code=403
        )

    agent_id = request.path_params["id"]
    registry = request.app.state.agent_registry
    pending_controls = request.app.state.pending_controls
    audit = getattr(request.app.state, "audit", None)

    entry = registry.get(agent_id)
    if entry is None:
        return JSONResponse({"error": "Agent not found"}, status_code=404)

    # Input validation
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    action = body.get("action")
    request_id = body.get("request_id")

    if not action or not request_id:
        return JSONResponse(
            {"error": "Missing required fields: action, request_id"},
            status_code=400,
        )

    try:
        msg = ControlMessage(
            action=action,
            target=agent_id,
            data=body.get("data", {}),
            request_id=request_id,
        )
    except Exception:
        return JSONResponse(
            {"error": "Invalid control message fields"}, status_code=400
        )

    # Create future for response correlation — store (target_id, future)
    future: asyncio.Future[ControlResponse] = asyncio.get_running_loop().create_future()
    pending_controls[msg.request_id] = (agent_id, future)

    if audit:
        audit.audit_event(
            "control.sent",
            {"agent_id": agent_id, "action": action, "request_id": request_id},
        )

    try:
        # Send to agent via WebSocket
        await entry.ws.send_json(msg.model_dump())

        # Wait for response with timeout
        result = await asyncio.wait_for(future, timeout=_CONTROL_TIMEOUT_SECONDS)
        if audit:
            audit.audit_event(
                "control.response",
                {"agent_id": agent_id, "request_id": request_id, "status": result.status},
            )
        return JSONResponse({"response": result.model_dump()})
    except TimeoutError:
        if audit:
            audit.audit_event(
                "control.timeout",
                {"agent_id": agent_id, "request_id": request_id},
            )
        return JSONResponse(
            {"error": "Agent did not respond in time"}, status_code=504
        )
    finally:
        pending_controls.pop(msg.request_id, None)


routes = [
    Route("/api/agents", list_agents, methods=["GET"]),
    Route("/api/agents/{id}", get_agent, methods=["GET"]),
    Route("/api/agents/{id}/control", control_agent, methods=["POST"]),
]
