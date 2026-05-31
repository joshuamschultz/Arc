"""Trace routes — /api/traces and /api/traces/{trace_id}."""

from __future__ import annotations

import re

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.schemas import ErrorResponse, TracesResponse

_MAX_TRACE_LIMIT = 500

# NIST SI-10: Input validation — same safe charset as _VALID_FILTER_RE.
# Trace IDs flow from many producers (chat_handler emits ``chat-NNN``,
# demo orchestrators emit ``run-abc:role``, ui_reporter aggregator emits
# ``trace-NNN-N``, arcllm emits hex UUIDs). All must round-trip through
# the detail route without being rejected.
_VALID_TRACE_ID_RE = re.compile(r"^[a-zA-Z0-9._:/-]{1,128}$")
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
        return JSONResponse(
            ErrorResponse(error="Invalid limit parameter").model_dump(mode="json"),
            status_code=400,
        )

    # Validate cursor format
    cursor_raw = params.get("cursor")
    if cursor_raw is not None and not _VALID_CURSOR_RE.match(cursor_raw):
        return JSONResponse(
            ErrorResponse(error="Invalid cursor format").model_dump(mode="json"),
            status_code=400,
        )

    # SPEC-026 FR-5: read LLM-call history from the arcstore mirror (durable),
    # not a live trace store. Reads are on-demand request/response.
    traces = await request.app.state.observe.traces(
        agent=_validate_filter(params.get("agent")),
        limit=limit,
    )
    return JSONResponse(TracesResponse(traces=traces, cursor=None).model_dump(mode="json"))


async def get_trace(request: Request) -> JSONResponse:
    """GET /api/traces/{trace_id} — get single trace by ID.

    Note: the response body for the single-trace path is the
    arcllm.TraceRecord ``model_dump()`` shape directly (not wrapped in
    an envelope) — kept untyped here because the producer owns that
    shape; tightening it would shadow the source of truth.
    """
    trace_id = request.path_params["trace_id"]

    # Validate trace_id format to prevent injection
    if not _VALID_TRACE_ID_RE.match(trace_id):
        return JSONResponse(
            ErrorResponse(error="Invalid trace ID format").model_dump(mode="json"),
            status_code=400,
        )

    record = await request.app.state.observe.trace(trace_id)
    if record is None:
        return JSONResponse(
            ErrorResponse(error="Trace not found").model_dump(mode="json"),
            status_code=404,
        )

    return JSONResponse(record)


routes = [
    Route("/api/traces", list_traces),
    Route("/api/traces/{trace_id}", get_trace),
]
