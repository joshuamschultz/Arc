"""Cost efficiency route — /api/cost-efficiency."""

from __future__ import annotations

import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

logger = logging.getLogger(__name__)

_VALID_WINDOWS = frozenset({"1h", "24h", "7d"})

_GONE_RESPONSE = JSONResponse(
    {"error": "Polling deprecated. Use /ws/dashboard."},
    status_code=410,
)


async def _publish(request: Request, topic: str, payload: Any) -> None:
    """Publish to the dashboard bus if wired (best-effort).

    SPEC-025 Track E — the route publishes its computed payload so the
    bus holds the latest known value for replay-on-subscribe.
    """
    bus = getattr(request.app.state, "dashboard_bus", None)
    if bus is None:
        return
    try:
        await bus.publish(topic, payload)
    except Exception:
        logger.debug(
            "cost_efficiency: dashboard_bus publish failed for topic=%s", topic, exc_info=True
        )


async def get_cost_efficiency(request: Request) -> JSONResponse:
    """GET /api/cost-efficiency — per-model cost efficiency ranking."""
    if not bool(getattr(request.app.state, "legacy_polling", True)):
        return _GONE_RESPONSE

    aggregator = request.app.state.aggregator
    if aggregator is None:
        return JSONResponse({"error": "No aggregator configured"}, status_code=404)

    window = request.query_params.get("window", "24h")
    if window not in _VALID_WINDOWS:
        return JSONResponse({"error": "Invalid window. Use 1h, 24h, or 7d."}, status_code=400)
    data = aggregator.cost_efficiency(window)
    await _publish(request, "cost_efficiency", data)
    return JSONResponse(data)


routes = [
    Route("/api/cost-efficiency", get_cost_efficiency),
]
