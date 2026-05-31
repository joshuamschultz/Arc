"""Cost efficiency route — /api/cost-efficiency.

SPEC-026 FR-5: computed on read from the arcstore mirror (``app.state.observe``),
not the deleted RollingAggregator.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.query_validators import safe_choice

logger = logging.getLogger(__name__)

_VALID_WINDOWS = frozenset({"1h", "24h", "7d"})


async def get_cost_efficiency(request: Request) -> JSONResponse:
    """GET /api/cost-efficiency — per-model cost efficiency ranking."""
    window, err = safe_choice(
        request.query_params.get("window", "24h"),
        _VALID_WINDOWS,
        error_label="Invalid window. Use 1h, 24h, or 7d.",
    )
    if err is not None:
        return err
    agent = request.query_params.get("agent_id")
    return JSONResponse(await request.app.state.observe.cost_efficiency(window, agent=agent))


routes = [
    Route("/api/cost-efficiency", get_cost_efficiency),
]
