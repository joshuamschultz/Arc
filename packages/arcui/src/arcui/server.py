"""ArcUI server — Starlette app factory and uvicorn runner.

create_app() builds the Starlette application with all routes and middleware.
serve() is the one-liner entry point for developers.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from arcui.aggregator import RollingAggregator
from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware, SessionTracker
from arcui.connection import ConnectionManager
from arcui.event_buffer import EventBuffer
from arcui.registry import AgentRegistry
from arcui.routes import agent_ws as agent_ws_routes
from arcui.routes import agents as agents_routes
from arcui.routes import arcllm_config as arcllm_config_routes
from arcui.routes import config as config_routes
from arcui.routes import cost_efficiency as cost_efficiency_routes
from arcui.routes import export as export_routes
from arcui.routes import stats as stats_routes
from arcui.routes import schedules as schedules_routes
from arcui.routes import traces as traces_routes
from arcui.routes import ws as ws_routes
from arcui.subscription import SubscriptionManager

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"


async def _health(request: Request) -> JSONResponse:
    """GET /api/health — simple health check."""
    return JSONResponse({"status": "ok"})


async def _agent_info(request: Request) -> JSONResponse:
    """GET /api/info — agent identity and metadata."""
    info = getattr(request.app.state, "agent_info", None) or {}
    return JSONResponse(info)


# Cache-Control header value used on every dashboard render. Wave 2
# review C-2 fix: a stale tab across `arc ui start` restart can resurrect
# dead viewer tokens and stale asset references; no-store prevents that.
_DASHBOARD_CACHE_CONTROL = "no-store, no-cache, must-revalidate"


async def _index(request: Request) -> HTMLResponse:
    """Serve the dashboard HTML from in-memory cache.

    The HTML is read once at app startup into `app.state.index_html`
    (Wave 2 perf fix). At one tab refresh per second across 5 tabs,
    serving from disk would be 5 × 77KB read+decode per second — the
    cache is a one-line change for a meaningful saving.

    `Cache-Control: no-store` still applies to the *browser* — the
    server cache is internal-only and invalidated by process restart.
    """
    headers = {"Cache-Control": _DASHBOARD_CACHE_CONTROL}
    cached = getattr(request.app.state, "index_html", None)
    if cached is not None:
        return HTMLResponse(cached, headers=headers)
    return HTMLResponse(
        "<h1>ArcUI</h1><p>Dashboard not found.</p>",
        status_code=404,
        headers=headers,
    )


def create_app(
    *,
    auth_config: AuthConfig | None = None,
    trace_store: Any | None = None,
    config_controller: Any | None = None,
    agent_info: dict[str, str] | None = None,
    max_agents: int = 100,
) -> Starlette:
    """Build a Starlette application with all ArcUI routes.

    Args:
        auth_config: Token/role configuration. Auto-generated if None.
        trace_store: ArcLLM JSONLTraceStore instance for trace queries.
        config_controller: ArcLLM ConfigController instance.
        agent_info: Agent metadata (name, did, model, provider) for UI display.
        max_agents: Maximum concurrent agent connections (default 100).

    Returns:
        Configured Starlette app, ready for uvicorn.
    """
    auth = auth_config or AuthConfig()

    routes = [
        Route("/", _index),
        Route("/api/health", _health),
        Route("/api/info", _agent_info),
        *traces_routes.routes,
        *schedules_routes.routes,
        *config_routes.routes,
        *arcllm_config_routes.routes,
        *stats_routes.routes,
        *export_routes.routes,
        *cost_efficiency_routes.routes,
        *ws_routes.routes,
        *agent_ws_routes.routes,
        *agents_routes.routes,
    ]

    # Mount static files if the directory exists
    if _STATIC_DIR.exists():
        routes.append(
            Mount(
                "/assets",
                app=StaticFiles(directory=str(_STATIC_DIR / "assets")),
            )
        )

    # Read index.html once at module-import time; serving from memory
    # avoids a 77KB disk read on every dashboard GET.
    index_path = _STATIC_DIR / "index.html"
    cached_index_html = (
        index_path.read_text() if index_path.exists() else None
    )

    # Starlette lifespan replaces the deprecated `on_startup=` parameter.
    # The async context manager runs the startup half before the server
    # accepts requests and the shutdown half during graceful shutdown.
    # Wave 2 review fix TD-04 — `on_startup=` produces deprecation
    # warnings in test runs and is a hard break on Starlette major bumps.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(starlette_app: Starlette) -> Any:
        starlette_app.state.event_buffer.start()
        # Browser-open callback (registered by `arc ui start` on
        # loopback) hooks the same lifespan via app.router.lifespan
        # extension below.
        for hook in getattr(starlette_app.state, "_extra_startup_hooks", []):
            await hook()
        try:
            yield
        finally:
            # No shutdown work today; reserved for future cleanup.
            pass

    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(AuthMiddleware, auth_config=auth)

    # Shared state accessible from routes via request.app.state
    connection_manager = ConnectionManager()
    aggregator = RollingAggregator()
    subscription_manager = SubscriptionManager()
    agent_registry = AgentRegistry(max_agents=max_agents)
    event_buffer = EventBuffer(
        connection_manager, subscription_manager=subscription_manager
    )

    app.state.index_html = cached_index_html
    app.state._extra_startup_hooks = []
    app.state.audit = UIAuditLogger()
    # SPEC-019 T5.3: tracker is consulted by AuthMiddleware to emit
    # `ui.session_start` exactly once per (token, remote_addr).
    app.state.session_tracker = SessionTracker()
    app.state.auth_config = auth
    app.state.trace_store = trace_store
    app.state.config_controller = config_controller
    app.state.connection_manager = connection_manager
    app.state.aggregator = aggregator
    app.state.event_buffer = event_buffer
    app.state.subscription_manager = subscription_manager
    app.state.agent_registry = agent_registry
    app.state.pending_controls = {}
    app.state.circuit_breakers = []
    app.state.telemetry_modules = []
    app.state.queue_modules = []
    # Bounded ring buffer of recent scheduler-layer UIEvents for warm-start
    # of the Schedule History card. Append-on-receive in agent_ws._receive.
    from collections import deque as _deque
    app.state.schedule_history = _deque(maxlen=50)
    app.state.on_event_callbacks = []
    app.state.agent_info = agent_info or {}

    return app


def attach_llm(app: Starlette, instance: Any, label: str | None = None) -> None:
    """Wire an LLM provider's events into the ArcUI pipeline.

    Creates an on_event callback that feeds:
      1. EventBuffer → ConnectionManager → WebSocket clients
      2. RollingAggregator → stats windows

    Also registers circuit breakers and telemetry modules for REST queries.

    Args:
        app: The Starlette app from create_app().
        instance: An LLMProvider (may be wrapped in module stack).
        label: Human-readable label for this LLM instance.
    """
    event_buffer: EventBuffer = app.state.event_buffer
    aggregator: RollingAggregator = app.state.aggregator

    def on_event(record: Any) -> None:
        data = record.model_dump() if hasattr(record, "model_dump") else record
        if label and "agent_label" not in data:
            data["agent_label"] = label
        event_buffer.push(data)
        aggregator.ingest(data)

    # Walk the module stack to find circuit breakers, telemetry, and queue modules
    try:
        from arcllm.modules.circuit_breaker import CircuitBreakerModule
        from arcllm.modules.queue import QueueModule
        from arcllm.modules.telemetry import TelemetryModule

        current = instance
        while current is not None:
            if isinstance(current, CircuitBreakerModule):
                app.state.circuit_breakers.append(current)
            if isinstance(current, TelemetryModule):
                app.state.telemetry_modules.append(current)
            if isinstance(current, QueueModule):
                app.state.queue_modules.append(current)
            current = getattr(current, "_inner", None)
    except ImportError:
        logger.debug("arcllm not available, skipping module stack discovery")

    app.state.on_event_callbacks.append(on_event)


def serve(
    llm: Any = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8420,
    trace_store: Any | None = None,
    config_controller: Any | None = None,
    auth_config: AuthConfig | None = None,
) -> None:
    """One-liner to start ArcUI dashboard.

    Usage::

        from arcui import serve
        serve(llm=model, trace_store=store)

    Args:
        llm: Optional LLMProvider to attach immediately.
        host: Bind address (default localhost).
        port: Port number (default 8420).
        trace_store: ArcLLM JSONLTraceStore for trace queries.
        config_controller: ArcLLM ConfigController for config management.
        auth_config: Auth configuration. Auto-generated if None.
    """
    import uvicorn

    app = create_app(
        auth_config=auth_config,
        trace_store=trace_store,
        config_controller=config_controller,
    )

    if llm is not None:
        attach_llm(app, llm)

    # Warm-start aggregator from existing trace data
    if trace_store is not None:
        import asyncio

        asyncio.run(app.state.aggregator.warm_start(trace_store))

    uvicorn.run(app, host=host, port=port, log_level="info")
