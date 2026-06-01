"""ArcUI server — Starlette app factory and uvicorn runner.

create_app() builds the Starlette application with all routes and middleware.
serve() is the one-liner entry point for developers.

SPEC-026 FR-5: arcui is a read-only consumer of the durable operational record.
The live push pipeline (``/ws`` feed, EventBuffer, SubscriptionManager,
RollingAggregator, the agent telemetry socket and the dashboard bus) is gone —
reads come from ``app.state.observe`` (an arcstore mirror) on demand. The only
WebSocket left is ``/ws/chat`` (interactive chat), which is bidirectional.
"""

from __future__ import annotations

import logging
from collections import deque as _deque
from pathlib import Path
from typing import Any

from arcgateway import team_roster
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware, SessionTracker
from arcui.observe import Observe
from arcui.registry import AgentRegistry
from arcui.routes import agent_detail as agent_detail_routes
from arcui.routes import agents as agents_routes
from arcui.routes import arcllm_config as arcllm_config_routes
from arcui.routes import chat_ws as chat_ws_routes
from arcui.routes import config as config_routes
from arcui.routes import cost_efficiency as cost_efficiency_routes
from arcui.routes import export as export_routes
from arcui.routes import knowledge as knowledge_routes
from arcui.routes import observe_run as observe_run_routes
from arcui.routes import stats as stats_routes
from arcui.routes import team_chat as team_chat_routes
from arcui.routes import team_pages as team_pages_routes
from arcui.routes import traces as traces_routes

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
    serving from disk would be 5 x 77KB read+decode per second — the
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


async def _service_worker(request: Request) -> Response:
    """Serve sw.js with `{{ARC_BUILD_ID}}` substituted at startup.

    SPEC-025 §TD-3 — without this, the SW's `CACHE_VERSION` constant is
    static and browsers serve cached shell across deploys. Routing through
    a Python handler (instead of StaticFiles) is what enables the
    template substitution that already runs for index.html.
    """
    cached = getattr(request.app.state, "sw_js", None)
    if cached is None:
        return Response("", status_code=404)
    # Service workers must be served with the right MIME and a no-cache
    # policy on the SW file itself; the cached *content* manages staleness.
    return Response(
        cached,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


def create_app(
    *,
    auth_config: AuthConfig | None = None,
    config_controller: Any | None = None,
    agent_info: dict[str, str] | None = None,
    max_agents: int = 100,
    team_root: Path | None = None,
    gateway_config: Any | None = None,
    messaging_service: Any | None = None,
    data_dir: Path | None = None,
) -> Starlette:
    """Build a Starlette application with all ArcUI routes.

    Args:
        auth_config: Token/role configuration. Auto-generated if None.
        config_controller: ArcLLM ConfigController instance.
        agent_info: Agent metadata (name, did, model, provider) for UI display.
        max_agents: Maximum concurrent agent connections (default 100).
        team_root: Path to ``team/`` directory containing ``<name>_agent/``
            subdirs. arcgateway.team_roster walks this to enumerate the fleet
            and arcgateway.fs_reader scopes file reads under each agent's
            workspace. ``None`` disables fleet/agent-detail endpoints — they
            return empty rosters and 404 for per-agent paths.
        gateway_config: Optional ``arcgateway.config.GatewayConfig``. When
            non-None and ``team_root`` is set, the lifespan composes an
            in-process gateway runtime (executor, session router, web/slack/
            telegram adapters) and exposes it via ``app.state``. SPEC-023.
        data_dir: Arc data directory for the Observe mirror (spool + WORM +
            store). Defaults to ``arcstore.config.resolve_data_dir()``.

    Returns:
        Configured Starlette app, ready for uvicorn.
    """
    auth = auth_config or AuthConfig()

    routes = [
        Route("/", _index),
        Route("/sw.js", _service_worker),
        Route("/api/health", _health),
        Route("/api/info", _agent_info),
        *traces_routes.routes,
        *config_routes.routes,
        *arcllm_config_routes.routes,
        *stats_routes.routes,
        *observe_run_routes.routes,
        *export_routes.routes,
        *cost_efficiency_routes.routes,
        *chat_ws_routes.routes,
        *knowledge_routes.routes,
        *agents_routes.routes,
        *agent_detail_routes.routes,
        *team_pages_routes.routes,
        *team_chat_routes.routes,
    ]

    # Mount static files if the directory exists.
    # `no-cache` (NOT `no-store`) means: browser MAY cache, but MUST
    # revalidate with a conditional request before reuse. Combined with
    # the per-startup cache-bust on asset URLs in index.html this gives
    # us belt-and-suspenders against the dev-loop bug where a restarted
    # server serves new HTML referencing new code but the browser keeps
    # the old JS in disk cache.
    class _NoCacheStaticFiles(StaticFiles):
        async def get_response(self, path: str, scope: Any) -> Any:
            resp = await super().get_response(path, scope)
            # Some response types (e.g. PlainTextResponse for 405) ship
            # immutable header dicts under specific Starlette versions; the
            # log-and-fall-through here is a defense-in-depth measure for
            # those edge cases. The asset will still serve, just without
            # the no-cache hint.
            try:
                resp.headers["Cache-Control"] = "no-cache"
            except Exception:  # reason: fail-open — log + continue
                logger.debug("could not set Cache-Control on static asset", exc_info=True)
            return resp

    if _STATIC_DIR.exists():
        routes.append(
            Mount(
                "/assets",
                app=_NoCacheStaticFiles(directory=str(_STATIC_DIR / "assets")),
            )
        )

    # Read index.html once at module-import time; serving from memory
    # avoids a 77KB disk read on every dashboard GET.
    #
    # SPEC-022 cache-bust: substitute `{{ARC_BUILD_ID}}` placeholders in
    # asset URLs with a per-process uuid so the browser refetches every
    # JS/CSS asset on every server restart. StaticFiles doesn't set
    # Cache-Control, so without this the browser happily reuses a cached
    # arc-shell.js across restarts and the user sees the old sidebar.
    index_path = _STATIC_DIR / "index.html"
    sw_path = _STATIC_DIR / "sw.js"
    if index_path.exists():
        import uuid as _uuid

        _build_id = _uuid.uuid4().hex[:12]
        cached_index_html = index_path.read_text().replace("{{ARC_BUILD_ID}}", _build_id)
        # SPEC-025 §TD-3 — sw.js gets the same template substitution so the
        # cache key bumps every process restart. Without this, browsers
        # serve the cached shell forever after asset changes.
        cached_sw_js: str | None = (
            sw_path.read_text().replace("{{ARC_BUILD_ID}}", _build_id)
            if sw_path.exists()
            else None
        )
    else:
        cached_index_html = None
        cached_sw_js = None

    # Starlette lifespan replaces the deprecated `on_startup=` parameter.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(starlette_app: Starlette) -> Any:
        # Observe plane: backfill the mirror from the durable spool + WORM, then
        # tail them. Fail-open — a store problem must never block the dashboard.
        try:
            await starlette_app.state.observe.start()
        except Exception:  # reason: fail-open — dashboard still serves
            logger.exception("lifespan: arcstore Observe failed to start; reads will be empty")
        # SPEC-023: when a gateway_config is supplied, compose the in-process
        # gateway runtime and expose its components on app.state. Routes that
        # need the WebPlatformAdapter (chat_ws), the SessionRouter (admin
        # tools) or the executor (introspection) read them from there.
        embedded_gateway = None
        if gateway_config is not None and team_root is not None:
            from arcgateway.bootstrap import build_for_embedded

            embedded_gateway = await build_for_embedded(team_root, gateway_config)
            starlette_app.state.embedded_gateway = embedded_gateway
            starlette_app.state.executor = embedded_gateway.executor
            starlette_app.state.session_router = embedded_gateway.session_router
            starlette_app.state.web_adapter = embedded_gateway.web_adapter
            starlette_app.state.stream_bridge = embedded_gateway.stream_bridge
            # SPEC-023: cache loaded agents and register them in the
            # fleet so chat-loaded agents show as LIVE without a separate
            # /api/agent/connect WebSocket. One install_ call, idempotent.
            from arcui.embedded_agents import install_embedded_agent_hooks

            install_embedded_agent_hooks(starlette_app)
            # Connect each adapter once so it's ready for inbound traffic.
            for adapter in (
                embedded_gateway.web_adapter,
                embedded_gateway.slack_adapter,
                embedded_gateway.telegram_adapter,
            ):
                if adapter is not None:
                    await adapter.connect()
        # Browser-open callback (registered by `arc ui start` on
        # loopback) hooks the same lifespan via app.router.lifespan
        # extension below.
        for hook in getattr(starlette_app.state, "_extra_startup_hooks", []):
            await hook()
        try:
            yield
        finally:
            try:
                await starlette_app.state.observe.stop()
            except Exception:  # reason: fail-open — continue shutdown
                logger.exception("lifespan: error stopping arcstore Observe")
            # SPEC-023: shut adapters down in reverse — disconnect cancels
            # all per-socket tasks and closes the WebSockets cleanly.
            if embedded_gateway is not None:
                for adapter in (
                    embedded_gateway.web_adapter,
                    embedded_gateway.slack_adapter,
                    embedded_gateway.telegram_adapter,
                ):
                    if adapter is None:
                        continue
                    try:
                        await adapter.disconnect()
                    except Exception:  # reason: fail-open — log + continue
                        logger.exception(
                            "lifespan: error disconnecting adapter %s",
                            getattr(adapter, "name", "unknown"),
                        )

    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(AuthMiddleware, auth_config=auth)

    # Shared state accessible from routes via request.app.state
    agent_registry = AgentRegistry(max_agents=max_agents)

    app.state.index_html = cached_index_html
    app.state.sw_js = cached_sw_js
    app.state._extra_startup_hooks = []
    app.state.audit = UIAuditLogger()
    # SPEC-019 T5.3: tracker is consulted by AuthMiddleware to emit
    # `ui.session_start` exactly once per (token, remote_addr).
    app.state.session_tracker = SessionTracker()
    app.state.auth_config = auth
    # Observe plane (SPEC-026 FR-5): arcui's read-only mirror of the durable
    # operational record. Reads come from here, not a live push wire.
    app.state.observe = Observe(data_dir=data_dir)
    app.state.config_controller = config_controller
    # arcteam MessagingService used by the Team Chat routes. ``None`` is
    # a supported state — the routes degrade to empty payloads so the
    # Team Chat tab never throws when the deployment lacks a team_root.
    app.state.messaging_service = messaging_service
    app.state.agent_registry = agent_registry
    app.state.pending_controls = {}
    app.state.circuit_breakers = []
    app.state.telemetry_modules = []
    app.state.queue_modules = []
    app.state.agent_info = agent_info or {}
    # SPEC-022 Phase 2: arcgateway data plane wiring. team_root scopes
    # all gateway fs reads; roster_provider walks it on each call so the
    # online overlay reflects the current AgentRegistry state. Routes
    # call this through app.state — they never import the registry or
    # team_root directly.
    app.state.team_root = team_root
    # Bounded ring of recent gateway/UI audit events surfaced through the
    # /api/agents/{id}/audit and fleet /api/team/audit endpoints.
    app.state.audit_buffer = _deque(maxlen=1000)

    def _roster_provider() -> list[team_roster.RosterEntry]:
        if app.state.team_root is None:
            return []
        # Registry and roster both key agents by `agent_name` (== the
        # arcagent.toml [agent].name == the directory name minus _agent).
        agents = app.state.agent_registry.list_agents()
        online = {a.agent_id for a in agents}
        return team_roster.list_team(team_root=app.state.team_root, online_ids=online)

    app.state.roster_provider = _roster_provider

    return app


def attach_llm(app: Starlette, instance: Any, label: str | None = None) -> None:
    """Register an LLM provider's modules for REST introspection.

    SPEC-026 FR-5: there is no event push anymore — telemetry is recorded to
    the arcstore spool by the arcllm client hook and read back through the
    Observe plane. This still walks the module stack so ``/api/circuit-breakers``
    / ``/api/budget`` / ``/api/queue`` can report live module state.

    Args:
        app: The Starlette app from create_app().
        instance: An LLMProvider (may be wrapped in module stack).
        label: Human-readable label for this LLM instance (unused; kept for
            call-site compatibility with the agent label passed by the CLI).
    """
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


def serve(
    llm: Any = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8420,
    config_controller: Any | None = None,
    auth_config: AuthConfig | None = None,
) -> None:
    """One-liner to start ArcUI dashboard.

    Usage::

        from arcui import serve
        serve(llm=model)

    Args:
        llm: Optional LLMProvider to attach immediately.
        host: Bind address (default localhost).
        port: Port number (default 8420).
        config_controller: ArcLLM ConfigController for config management.
        auth_config: Auth configuration. Auto-generated if None.
    """
    import uvicorn

    app = create_app(
        auth_config=auth_config,
        config_controller=config_controller,
    )

    if llm is not None:
        attach_llm(app, llm)

    uvicorn.run(app, host=host, port=port, log_level="info")
