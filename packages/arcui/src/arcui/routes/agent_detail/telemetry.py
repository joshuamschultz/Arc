"""`/api/agents/{id}/{stats,traces,audit}` route handlers."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.query_validators import safe_choice, safe_int
from arcui.routes.agent_detail._common import _agent_did, _agent_root
from arcui.schemas import (
    AuditEventsResponse,
    ErrorResponse,
    StatsResponse,
    TracesResponse,
)


async def get_stats(request: Request) -> JSONResponse:
    """Per-agent stats — computed on read from the arcstore mirror.

    Mirrors ``/api/stats?agent_id=`` but uses the path-param style for symmetry
    with the agent-detail screen (SPEC-026 FR-5).
    """
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse(
            ErrorResponse(error="Agent not found").model_dump(mode="json"),
            status_code=404,
        )

    window, err = safe_choice(
        request.query_params.get("window", "24h"),
        {"1h", "24h", "7d"},
        error_label="Invalid window",
    )
    if err is not None:
        return err
    stats = await request.app.state.observe.stats(window, agent=agent_id)
    return JSONResponse(StatsResponse(stats=stats, window=window).model_dump(mode="json"))


async def get_traces(request: Request) -> JSONResponse:
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse(
            ErrorResponse(error="Agent not found").model_dump(mode="json"),
            status_code=404,
        )

    limit, err = safe_int(
        request.query_params.get("limit"),
        default=50,
        min_=1,
        max_=500,
        error_label="Invalid limit",
    )
    if err is not None:
        return err

    traces = await request.app.state.observe.traces(agent=agent_id, limit=limit)
    return JSONResponse(TracesResponse(traces=traces, cursor=None).model_dump(mode="json"))


async def get_audit(request: Request) -> JSONResponse:
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse(
            ErrorResponse(error="Agent not found").model_dump(mode="json"),
            status_code=404,
        )

    limit, err = safe_int(
        request.query_params.get("limit"),
        default=100,
        min_=1,
        max_=1000,
        error_label="Invalid limit",
    )
    if err is not None:
        return err

    did = _agent_did(request, agent_id)
    events = await request.app.state.observe.audit(agent=did, limit=limit) if did else []
    return JSONResponse(AuditEventsResponse(events=events).model_dump(mode="json"))
