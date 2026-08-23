"""Authenticated operator controls for live connected-data synchronization."""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.audit import emit_mutation_audit
from arcui.routes.agent_detail._common import _agent_did
from arcui.schemas import ErrorResponse


def _agent(request: Request, agent_id: str) -> Any | None:
    cache = getattr(request.app.state, "embedded_agent_cache", None)
    did = _agent_did(request, agent_id)
    return cache.get(did) if cache is not None and did is not None else None


async def _service(request: Request, agent_id: str) -> Any | None:
    agent = _agent(request, agent_id)
    registry = getattr(agent, "_capability_registry", None)
    if registry is None:
        return None
    entry = await registry.get_capability("connected_data")
    return getattr(getattr(entry, "instance", None), "service", None)


def _operator(request: Request) -> JSONResponse | None:
    if getattr(request.state, "role", None) != "operator":
        return JSONResponse(
            ErrorResponse(error="Operator role required").model_dump(), status_code=403
        )
    return None


async def sync_status(request: Request) -> JSONResponse:
    """List safe source synchronization status; no cursor or content is returned."""
    service = await _service(request, request.path_params["agent_id"])
    if service is None:
        return JSONResponse({"items": [], "status": "degraded"})
    items = []
    for status in await service.list_status():
        state = status.state
        items.append(
            {
                "connection_id": status.connection_id,
                "status": status.status,
                "detail": status.detail,
                "pages": state.pages if state else 0,
                "bytes_processed": state.bytes_processed if state else 0,
                "error_code": state.error_code if state else None,
            }
        )
    return JSONResponse({"items": items})


async def sync_action(request: Request) -> JSONResponse:
    denied = _operator(request)
    if denied is not None:
        return denied
    agent_id = request.path_params["agent_id"]
    source_id = request.path_params["source_id"]
    action = request.path_params["action"]
    service = await _service(request, agent_id)
    if service is None:
        return JSONResponse(
            ErrorResponse(error="connected-data module unavailable").model_dump(), status_code=503
        )
    operations = {
        "sync": service.sync_now,
        "retry": service.sync_now,
        "pause": service.pause,
        "resume": service.resume,
        "revoke": service.revoke,
    }
    operation = operations.get(action)
    if operation is None:
        return JSONResponse(
            ErrorResponse(error="unsupported sync action").model_dump(), status_code=400
        )
    applied = await operation(source_id)
    emit_mutation_audit(
        request,
        target=f"agent:{agent_id}/source:{source_id}",
        operation=f"connected_data.{action}",
        outcome="applied" if applied else "not_found",
    )
    if not applied:
        return JSONResponse(ErrorResponse(error="source not found").model_dump(), status_code=404)
    return JSONResponse({"status": "accepted", "action": action, "source_id": source_id})


routes = [
    Route("/api/agents/{agent_id}/knowledge/sync", sync_status, methods=["GET"]),
    Route(
        "/api/agents/{agent_id}/knowledge/sync/{source_id}/{action}",
        sync_action,
        methods=["POST"],
    ),
]

__all__ = ["routes", "sync_action", "sync_status"]
