"""`/api/agents/{id}/{stats,traces,audit}` route handlers."""

from __future__ import annotations

from collections import deque
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from arcui.query_validators import safe_choice, safe_int
from arcui.routes.agent_detail._common import _agent_root
from arcui.schemas import (
    AuditEventsResponse,
    ErrorResponse,
    StatsResponse,
    TracesResponse,
)


async def get_stats(request: Request) -> JSONResponse:
    """Per-agent stats — delegates to per-agent or global aggregator.

    Mirrors the behaviour of the existing ``/api/stats?agent_id=`` route but
    uses the path-param style for symmetry with the agent-detail screen.
    """
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse(
            ErrorResponse(error="Agent not found").model_dump(mode="json"),
            status_code=404,
        )

    registry = request.app.state.agent_registry
    entry = registry.get(agent_id)
    aggregator = (
        entry.aggregator
        if entry and entry.aggregator
        else getattr(request.app.state, "aggregator", None)
    )
    if aggregator is None:
        return JSONResponse(StatsResponse(stats={}, window="24h").model_dump(mode="json"))
    window, err = safe_choice(
        request.query_params.get("window", "24h"),
        {"1h", "24h", "7d"},
        error_label="Invalid window",
    )
    if err is not None:
        return err
    return JSONResponse(
        StatsResponse(
            stats=aggregator.stats(window),
            window=window,
        ).model_dump(mode="json")
    )


async def get_traces(request: Request) -> JSONResponse:
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse(
            ErrorResponse(error="Agent not found").model_dump(mode="json"),
            status_code=404,
        )

    store = request.app.state.trace_store
    if store is None:
        return JSONResponse(TracesResponse(traces=[], cursor=None).model_dump(mode="json"))

    limit, err = safe_int(
        request.query_params.get("limit"),
        default=50,
        min_=1,
        max_=500,
        error_label="Invalid limit",
    )
    if err is not None:
        return err

    records, cursor = await store.query(limit=limit, agent=agent_id)
    return JSONResponse(
        TracesResponse(
            traces=[r.model_dump() for r in records],
            cursor=cursor,
        ).model_dump(mode="json")
    )


async def get_audit(request: Request) -> JSONResponse:
    agent_id = request.path_params["id"]
    agent_root = _agent_root(request, agent_id)
    if agent_root is None:
        return JSONResponse(
            ErrorResponse(error="Agent not found").model_dump(mode="json"),
            status_code=404,
        )

    buffer: deque[dict[str, Any]] = getattr(request.app.state, "audit_buffer", None) or deque()
    limit, err = safe_int(
        request.query_params.get("limit"),
        default=100,
        min_=1,
        max_=1000,
        error_label="Invalid limit",
    )
    if err is not None:
        return err

    events = [e for e in buffer if e.get("agent_id") == agent_id][-limit:]
    return JSONResponse(AuditEventsResponse(events=events).model_dump(mode="json"))
