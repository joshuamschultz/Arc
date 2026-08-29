"""Cost efficiency route — /api/cost-efficiency.

SPEC-026 FR-5: computed on read from the arcstore mirror (``app.state.observe``),
not the deleted RollingAggregator.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.identity import resolve_agent_did
from arcui.query_validators import safe_choice

logger = logging.getLogger(__name__)

_VALID_WINDOWS = frozenset({"1h", "24h", "7d"})


async def get_cost_efficiency(request: Request) -> JSONResponse:
    """GET /api/cost-efficiency — per-model cost efficiency ranking.

    Supports ``?agent_id=`` for per-agent scoping, resolved to the agent's DID
    before filtering (H-008) — see ``arcui.routes.stats._agent_filter``.
    """
    window, err = safe_choice(
        request.query_params.get("window", "24h"),
        _VALID_WINDOWS,
        error_label="Invalid window. Use 1h, 24h, or 7d.",
    )
    if err is not None:
        return err
    agent = request.query_params.get("agent_id")
    resolved_agent = None
    if agent:
        provider = getattr(request.app.state, "roster_provider", None)
        resolved = resolve_agent_did(provider(), agent) if provider is not None else None
        resolved_agent = resolved or agent
    return JSONResponse(
        await request.app.state.observe.cost_efficiency(window, agent=resolved_agent)
    )


routes = [
    Route("/api/cost-efficiency", get_cost_efficiency),
]
