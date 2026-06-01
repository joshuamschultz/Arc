"""Stats routes — /api/stats, /api/circuit-breakers, /api/budget.

SPEC-026 FR-5: the rolling-aggregator push pipeline is gone. ``/api/stats``,
``/api/stats/timeseries`` and ``/api/performance`` recompute on read from the
arcstore mirror (``app.state.observe``). ``?agent_id=`` scopes to one agent's
rows. Circuit-breaker / budget / queue endpoints read live arcllm module state
directly (not telemetry history) and are unchanged.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.schemas import ErrorResponse

logger = logging.getLogger(__name__)

# NIST SI-10: Allowlist valid window values at the API boundary
_VALID_WINDOWS = frozenset({"1h", "24h", "7d", "30d"})


def _validated_window(request: Request) -> str | None:
    """Extract and validate the window query param. Returns None if invalid."""
    window = request.query_params.get("window", "24h")
    if window not in _VALID_WINDOWS:
        return None
    return window


def _invalid_window_response() -> JSONResponse:
    return JSONResponse(
        ErrorResponse(error="Invalid window. Use 1h, 24h, 7d, or 30d.").model_dump(mode="json"),
        status_code=400,
    )


async def get_stats(request: Request) -> JSONResponse:
    """GET /api/stats — telemetry rollup over a window from the store.

    Supports ``?agent_id=`` for per-agent drill-down (filters on agent_label).
    """
    window = _validated_window(request)
    if window is None:
        return _invalid_window_response()
    agent = request.query_params.get("agent_id")
    return JSONResponse(await request.app.state.observe.stats(window, agent=agent))


async def get_timeseries(request: Request) -> JSONResponse:
    """GET /api/stats/timeseries — per-bucket series for chart rendering."""
    window = _validated_window(request)
    if window is None:
        return _invalid_window_response()
    agent = request.query_params.get("agent_id")
    return JSONResponse(await request.app.state.observe.timeseries(window, agent=agent))


async def get_circuit_breakers(request: Request) -> JSONResponse:
    """GET /api/circuit-breakers — list circuit breaker states."""
    breakers = request.app.state.circuit_breakers or []
    return JSONResponse({"circuit_breakers": [cb.get_state() for cb in breakers]})


async def get_budget(request: Request) -> JSONResponse:
    """GET /api/budget — list budget states from telemetry modules."""
    telemetry_modules = request.app.state.telemetry_modules or []
    budgets = [s for tm in telemetry_modules if (s := tm.get_budget_state()) is not None]
    return JSONResponse({"budgets": budgets})


async def get_performance(request: Request) -> JSONResponse:
    """GET /api/performance — per-model/per-agent performance from the store.

    Supports ``?agent_id=`` for per-agent drill-down.
    """
    window = _validated_window(request)
    if window is None:
        return _invalid_window_response()
    agent = request.query_params.get("agent_id")
    return JSONResponse(await request.app.state.observe.performance(window, agent=agent))


async def get_queue_stats(request: Request) -> JSONResponse:
    """GET /api/queue — queue module stats (depth, wait times, rejections)."""
    queue_modules = getattr(request.app.state, "queue_modules", []) or []
    return JSONResponse({"queues": [qm.queue_stats() for qm in queue_modules]})


routes = [
    Route("/api/stats", get_stats),
    Route("/api/stats/timeseries", get_timeseries),
    Route("/api/circuit-breakers", get_circuit_breakers),
    Route("/api/budget", get_budget),
    Route("/api/performance", get_performance),
    Route("/api/queue", get_queue_stats),
]
