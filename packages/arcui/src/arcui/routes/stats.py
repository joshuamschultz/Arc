"""Stats routes — /api/stats, /api/circuit-breakers, /api/budget."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

# NIST SI-10: Allowlist valid window values at the API boundary
_VALID_WINDOWS = frozenset({"1h", "24h", "7d"})


def _validated_window(request: Request) -> str | None:
    """Extract and validate the window query param. Returns None if invalid."""
    window = request.query_params.get("window", "24h")
    if window not in _VALID_WINDOWS:
        return None
    return window


async def get_stats(request: Request) -> JSONResponse:
    """GET /api/stats — rolling aggregation stats."""
    aggregator = request.app.state.aggregator
    if aggregator is None:
        return JSONResponse({"error": "No aggregator configured"}, status_code=404)

    window = _validated_window(request)
    if window is None:
        return JSONResponse({"error": "Invalid window. Use 1h, 24h, or 7d."}, status_code=400)
    return JSONResponse(aggregator.stats(window))


async def get_timeseries(request: Request) -> JSONResponse:
    """GET /api/stats/timeseries — per-bucket data for chart rendering."""
    aggregator = request.app.state.aggregator
    if aggregator is None:
        return JSONResponse({"error": "No aggregator configured"}, status_code=404)

    window = _validated_window(request)
    if window is None:
        return JSONResponse({"error": "Invalid window. Use 1h, 24h, or 7d."}, status_code=400)
    return JSONResponse(aggregator.timeseries(window))


async def get_circuit_breakers(request: Request) -> JSONResponse:
    """GET /api/circuit-breakers — list circuit breaker states."""
    breakers = request.app.state.circuit_breakers or []
    states = [cb.get_state() for cb in breakers]
    return JSONResponse({"circuit_breakers": states})


async def get_budget(request: Request) -> JSONResponse:
    """GET /api/budget — list budget states from telemetry modules."""
    telemetry_modules = request.app.state.telemetry_modules or []
    budgets = []
    for tm in telemetry_modules:
        state = tm.get_budget_state()
        if state is not None:
            budgets.append(state)
    return JSONResponse({"budgets": budgets})


async def get_performance(request: Request) -> JSONResponse:
    """GET /api/performance — per-model and per-agent performance stats."""
    aggregator = request.app.state.aggregator
    if aggregator is None:
        return JSONResponse({"error": "No aggregator configured"}, status_code=404)

    window = _validated_window(request)
    if window is None:
        return JSONResponse({"error": "Invalid window. Use 1h, 24h, or 7d."}, status_code=400)
    return JSONResponse(aggregator.performance(window))


async def get_queue_stats(request: Request) -> JSONResponse:
    """GET /api/queue — queue module stats (depth, wait times, rejections)."""
    queue_modules = getattr(request.app.state, "queue_modules", []) or []
    queues = [qm.queue_stats() for qm in queue_modules]
    return JSONResponse({"queues": queues})


routes = [
    Route("/api/stats", get_stats),
    Route("/api/stats/timeseries", get_timeseries),
    Route("/api/circuit-breakers", get_circuit_breakers),
    Route("/api/budget", get_budget),
    Route("/api/performance", get_performance),
    Route("/api/queue", get_queue_stats),
]
