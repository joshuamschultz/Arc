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
    # H-008: join on the agent's DID, not the roster label — a label can
    # drift from whatever string got recorded on its llm_calls historically.
    # Fall back to the raw id (never None) when the roster is present but
    # this agent has no DID on file, so the read still narrows to empty
    # rather than reading every agent's rows.
    stats = await request.app.state.observe.stats(
        window, agent=_agent_did(request, agent_id) or agent_id
    )
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

    traces = await request.app.state.observe.traces(
        agent=_agent_did(request, agent_id) or agent_id, limit=limit
    )
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
