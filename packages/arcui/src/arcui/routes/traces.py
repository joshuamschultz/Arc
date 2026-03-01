"""Trace routes — /api/traces and /api/traces/{trace_id}."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

_MAX_TRACE_LIMIT = 500


def _parse_limit(raw: str, default: int, ceiling: int) -> int | None:
    """Parse and clamp a limit parameter. Returns None on invalid input."""
    try:
        return max(1, min(ceiling, int(raw)))
    except (ValueError, TypeError):
        return None


async def list_traces(request: Request) -> JSONResponse:
    """GET /api/traces — query TraceStore with filters."""
    params = request.query_params

    limit = _parse_limit(params.get("limit", "50"), 50, _MAX_TRACE_LIMIT)
    if limit is None:
        return JSONResponse({"error": "Invalid limit parameter"}, status_code=400)

    store = request.app.state.trace_store
    if store is None:
        return JSONResponse({"traces": [], "cursor": None})

    records, cursor = await store.query(
        limit=limit,
        cursor=params.get("cursor"),
        provider=params.get("provider"),
        agent=params.get("agent"),
        status=params.get("status"),
        start=params.get("start"),
        end=params.get("end"),
    )
    return JSONResponse({
        "traces": [r.model_dump() for r in records],
        "cursor": cursor,
    })


async def get_trace(request: Request) -> JSONResponse:
    """GET /api/traces/{trace_id} — get single trace by ID."""
    store = request.app.state.trace_store
    trace_id = request.path_params["trace_id"]

    if store is None:
        return JSONResponse({"error": "No trace store configured"}, status_code=404)

    record = await store.get(trace_id)
    if record is None:
        return JSONResponse({"error": "Trace not found"}, status_code=404)

    return JSONResponse(record.model_dump())


routes = [
    Route("/api/traces", list_traces),
    Route("/api/traces/{trace_id}", get_trace),
]
