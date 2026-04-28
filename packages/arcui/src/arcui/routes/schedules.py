"""Schedule history routes — /api/schedule-history.

Read-only access to the bounded server-side ring buffer of recent scheduler-layer
UIEvents (schedule:completed, schedule:failed). Used by the Schedule History
dashboard card to warm-start on page load — without this endpoint the card
only shows fires that happen while the tab is open.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

_DEFAULT_LIMIT = 5
_MAX_LIMIT = 50


def _parse_limit(raw: str | None) -> int:
    """Parse an optional ?limit= query param. Clamped to [1, _MAX_LIMIT]."""
    if not raw:
        return _DEFAULT_LIMIT
    try:
        return max(1, min(_MAX_LIMIT, int(raw)))
    except (ValueError, TypeError):
        return _DEFAULT_LIMIT


async def schedule_history(request: Request) -> JSONResponse:
    """GET /api/schedule-history — recent schedule:completed + schedule:failed events.

    Returns the last N (default 5) scheduler-layer events from the server-side
    ring buffer in newest-first order.
    """
    history = getattr(request.app.state, "schedule_history", None)
    if history is None:
        return JSONResponse({"events": []})
    limit = _parse_limit(request.query_params.get("limit"))
    # deque is oldest→newest; return newest-first.
    events = list(history)[-limit:][::-1]
    return JSONResponse({"events": events})


routes = [
    Route("/api/schedule-history", schedule_history),
]
