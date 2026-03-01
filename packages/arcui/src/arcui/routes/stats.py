"""Stats routes — /api/stats, /api/circuit-breakers, /api/budget."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


async def get_stats(request: Request) -> JSONResponse:
    """GET /api/stats — rolling aggregation stats."""
    aggregator = request.app.state.aggregator
    if aggregator is None:
        return JSONResponse({"error": "No aggregator configured"}, status_code=404)

    window = request.query_params.get("window", "24h")
    return JSONResponse(aggregator.stats(window))


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


routes = [
    Route("/api/stats", get_stats),
    Route("/api/circuit-breakers", get_circuit_breakers),
    Route("/api/budget", get_budget),
]
