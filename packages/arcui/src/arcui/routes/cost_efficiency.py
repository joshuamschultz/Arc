"""Cost efficiency route — /api/cost-efficiency."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


async def get_cost_efficiency(request: Request) -> JSONResponse:
    """GET /api/cost-efficiency — per-model cost efficiency ranking."""
    aggregator = request.app.state.aggregator
    if aggregator is None:
        return JSONResponse({"error": "No aggregator configured"}, status_code=404)

    window = request.query_params.get("window", "24h")
    return JSONResponse(aggregator.cost_efficiency(window))


routes = [
    Route("/api/cost-efficiency", get_cost_efficiency),
]
