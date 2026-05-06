"""Stats routes — /api/stats, /api/circuit-breakers, /api/budget."""

from __future__ import annotations

import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arcui.aggregator import RollingAggregator

logger = logging.getLogger(__name__)

# NIST SI-10: Allowlist valid window values at the API boundary
_VALID_WINDOWS = frozenset({"1h", "24h", "7d", "30d"})

_GONE_RESPONSE = JSONResponse(
    {"error": "Polling deprecated. Use /ws/dashboard."},
    status_code=410,
)


# TODO(SPEC-025 Track E cleanup): when ARCUI_LEGACY_POLLING flips off
# in the next release, every `_legacy_polling_enabled(request)` call
# below — and this helper — gets removed in a single mechanical pass.
# The 6 routes in this file plus `cost_efficiency.py` and `schedules.py`
# are the call sites; grep `_legacy_polling_enabled` to find them all.
def _legacy_polling_enabled(request: Request) -> bool:
    """Return True when polling endpoints should serve data (default)."""
    return bool(getattr(request.app.state, "legacy_polling", True))


def _validated_window(request: Request) -> str | None:
    """Extract and validate the window query param. Returns None if invalid."""
    window = request.query_params.get("window", "24h")
    if window not in _VALID_WINDOWS:
        return None
    return window


def _get_aggregator_for_request(
    request: Request,
) -> tuple[RollingAggregator | None, JSONResponse | None]:
    """Return the appropriate aggregator: per-agent or global.

    If ``?agent_id=`` is provided, looks up the per-agent aggregator
    from the agent registry. For an agent that's known on disk but not
    currently connected, returns an empty aggregator (200 with zero
    counts) — the agent-detail page renders "no activity yet" instead
    of error-ing on a 404 just because the agent is offline.
    """
    agent_id = request.query_params.get("agent_id")
    if agent_id is not None:
        registry = getattr(request.app.state, "agent_registry", None)
        if registry is None:
            return None, JSONResponse({"error": "Agent registry not available"}, status_code=404)
        entry = registry.get(agent_id)
        if entry is None or entry.aggregator is None:
            # Offline agent (or one whose aggregator wasn't initialised
            # yet): synthesise an empty per-call aggregator so callers
            # get an empty-but-well-formed response.
            return RollingAggregator(), None
        return entry.aggregator, None
    return request.app.state.aggregator, None


async def _publish(request: Request, topic: str, payload: Any) -> None:
    """Publish to the dashboard bus if wired (best-effort).

    SPEC-025 Track E — each polling handler publishes its computed
    payload so the bus always holds the latest known value, and
    /ws/dashboard subscribers receive push updates without polling.
    """
    bus = getattr(request.app.state, "dashboard_bus", None)
    if bus is None:
        return
    try:
        await bus.publish(topic, payload)
    except Exception:
        logger.debug("stats: dashboard_bus publish failed for topic=%s", topic, exc_info=True)


async def get_stats(request: Request) -> JSONResponse:
    """GET /api/stats — rolling aggregation stats.

    Supports ``?agent_id=`` for per-agent drill-down.
    """
    if not _legacy_polling_enabled(request):
        return _GONE_RESPONSE

    aggregator, err = _get_aggregator_for_request(request)
    if err is not None:
        return err
    if aggregator is None:
        return JSONResponse({"error": "No aggregator configured"}, status_code=404)

    window = _validated_window(request)
    if window is None:
        return JSONResponse({"error": "Invalid window. Use 1h, 24h, 7d, or 30d."}, status_code=400)
    data = aggregator.stats(window)
    await _publish(request, "stats", data)
    return JSONResponse(data)


async def get_timeseries(request: Request) -> JSONResponse:
    """GET /api/stats/timeseries — per-bucket data for chart rendering.

    Supports ``?agent_id=`` for per-agent drill-down.
    """
    if not _legacy_polling_enabled(request):
        return _GONE_RESPONSE

    aggregator, err = _get_aggregator_for_request(request)
    if err is not None:
        return err
    if aggregator is None:
        return JSONResponse({"error": "No aggregator configured"}, status_code=404)

    window = _validated_window(request)
    if window is None:
        return JSONResponse({"error": "Invalid window. Use 1h, 24h, 7d, or 30d."}, status_code=400)
    data = aggregator.timeseries(window)
    await _publish(request, "stats.timeseries", data)
    return JSONResponse(data)


async def get_circuit_breakers(request: Request) -> JSONResponse:
    """GET /api/circuit-breakers — list circuit breaker states."""
    if not _legacy_polling_enabled(request):
        return _GONE_RESPONSE

    breakers = request.app.state.circuit_breakers or []
    states = [cb.get_state() for cb in breakers]
    data = {"circuit_breakers": states}
    await _publish(request, "circuit_breakers", data)
    return JSONResponse(data)


async def get_budget(request: Request) -> JSONResponse:
    """GET /api/budget — list budget states from telemetry modules."""
    if not _legacy_polling_enabled(request):
        return _GONE_RESPONSE

    telemetry_modules = request.app.state.telemetry_modules or []
    budgets = []
    for tm in telemetry_modules:
        state = tm.get_budget_state()
        if state is not None:
            budgets.append(state)
    data = {"budgets": budgets}
    await _publish(request, "budget", data)
    return JSONResponse(data)


async def get_performance(request: Request) -> JSONResponse:
    """GET /api/performance — per-model and per-agent performance stats.

    Supports ``?agent_id=`` for per-agent drill-down.
    """
    if not _legacy_polling_enabled(request):
        return _GONE_RESPONSE

    aggregator, err = _get_aggregator_for_request(request)
    if err is not None:
        return err
    if aggregator is None:
        return JSONResponse({"error": "No aggregator configured"}, status_code=404)

    window = _validated_window(request)
    if window is None:
        return JSONResponse({"error": "Invalid window. Use 1h, 24h, 7d, or 30d."}, status_code=400)
    data = aggregator.performance(window)
    await _publish(request, "performance", data)
    return JSONResponse(data)


async def get_queue_stats(request: Request) -> JSONResponse:
    """GET /api/queue — queue module stats (depth, wait times, rejections)."""
    if not _legacy_polling_enabled(request):
        return _GONE_RESPONSE

    queue_modules = getattr(request.app.state, "queue_modules", []) or []
    queues = [qm.queue_stats() for qm in queue_modules]
    data = {"queues": queues}
    await _publish(request, "queue", data)
    return JSONResponse(data)


routes = [
    Route("/api/stats", get_stats),
    Route("/api/stats/timeseries", get_timeseries),
    Route("/api/circuit-breakers", get_circuit_breakers),
    Route("/api/budget", get_budget),
    Route("/api/performance", get_performance),
    Route("/api/queue", get_queue_stats),
]
