"""Trace routes — /api/traces and /api/traces/{trace_id}."""

from __future__ import annotations

import re

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

_MAX_TRACE_LIMIT = 500

# NIST SI-10: Input validation — trace IDs are hex UUIDs (32 hex chars)
_VALID_TRACE_ID_RE = re.compile(r"^[a-f0-9]{32}$")
# Cursor format: YYYY-MM-DD:line_number
_VALID_CURSOR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}:\d+$")
# Filter params: alphanumeric + dash/underscore/dot/colon (safe chars only)
_VALID_FILTER_RE = re.compile(r"^[a-zA-Z0-9._:/-]{1,128}$")


def _parse_limit(raw: str, default: int, ceiling: int) -> int | None:
    """Parse and clamp a limit parameter. Returns None on invalid input."""
    try:
        return max(1, min(ceiling, int(raw)))
    except (ValueError, TypeError):
        return None


def _validate_filter(value: str | None) -> str | None:
    """Validate optional filter param. Returns None if invalid."""
    if value is None:
        return None
    if not _VALID_FILTER_RE.match(value):
        return None
    return value


async def list_traces(request: Request) -> JSONResponse:
    """GET /api/traces — query TraceStore with filters."""
    params = request.query_params

    limit = _parse_limit(params.get("limit", "50"), 50, _MAX_TRACE_LIMIT)
    if limit is None:
        return JSONResponse({"error": "Invalid limit parameter"}, status_code=400)

    # Validate cursor format
    cursor_raw = params.get("cursor")
    if cursor_raw is not None and not _VALID_CURSOR_RE.match(cursor_raw):
        return JSONResponse({"error": "Invalid cursor format"}, status_code=400)

    store = request.app.state.trace_store
    if store is None:
        return JSONResponse({"traces": [], "cursor": None})

    records, cursor = await store.query(
        limit=limit,
        cursor=cursor_raw,
        provider=_validate_filter(params.get("provider")),
        agent=_validate_filter(params.get("agent")),
        status=_validate_filter(params.get("status")),
        start=_validate_filter(params.get("start")),
        end=_validate_filter(params.get("end")),
    )
    return JSONResponse(
        {
            "traces": [r.model_dump() for r in records],
            "cursor": cursor,
        }
    )


async def get_trace(request: Request) -> JSONResponse:
    """GET /api/traces/{trace_id} — get single trace by ID."""
    trace_id = request.path_params["trace_id"]

    # Validate trace_id format to prevent injection
    if not _VALID_TRACE_ID_RE.match(trace_id):
        return JSONResponse({"error": "Invalid trace ID format"}, status_code=400)

    store = request.app.state.trace_store
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
