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

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

from arcgateway import team_roster
from arcstore.backends.sqlite import SqliteBackend
from arcstore.config import resolve_data_dir
from arcstore.tasks import TaskStore
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
from arcui.routes import agent_sessions as agent_sessions_routes
from arcui.routes import agents as agents_routes
from arcui.routes import arcllm_config as arcllm_config_routes
from arcui.routes import chat_ws as chat_ws_routes
from arcui.routes import config as config_routes
from arcui.routes import cost_efficiency as cost_efficiency_routes
from arcui.routes import export as export_routes
from arcui.routes import knowledge as knowledge_routes
from arcui.routes import observe_run as observe_run_routes
from arcui.routes import stats as stats_routes
from arcui.routes import tasks as tasks_routes
from arcui.routes import team_chat as team_chat_routes
from arcui.routes import team_pages as team_pages_routes
from arcui.routes import team_ws as team_ws_routes
from arcui.routes import traces as traces_routes
from arcui.team_stream import TeamBusObserver, TeamStreamHub

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
    team_post_forwarder: Any | None = None,
    team_stream_interval: float = 1.0,
    data_dir: Path | None = None,
    workspace_dir: Path | None = None,
    allow_external_task_refs: bool = False,
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
        team_post_forwarder: Optional async callable
            ``(*, sender, channel, text) -> None`` that hands a human group post
            to arcteam, which signs and routes it as that human entity (REQ-061).
            arcui only forwards — it never signs or routes. ``None`` disables
            posting (the ``/ws/team`` route replies with an error frame).
        team_stream_interval: Seconds between :class:`TeamBusObserver` polls of
            the arcteam bus that feed the read-only ``/ws/team`` stream.
        data_dir: Arc data directory for the Observe mirror (spool + WORM +
            store). Defaults to ``arcstore.config.resolve_data_dir()``.
        workspace_dir: Agent workspace root for the Observe ingest's arcskill
            candidate-store + skills-WORM scan (SPEC-054 REQ-120). ``None``
            keeps the mirror on spool + audit WORM only.
        allow_external_task_refs: Ingest policy for operator-authored task text
            (ADR-019 tier = stringency). Federal → False (default): URLs/emails
            in a task title/description are rejected as an external-comms
            surface. Personal/enterprise → True: pointing an agent at a repo or
            doc is a core use. Never gates reads of already-persisted tasks.

    Returns:
        Configured Starlette app, ready for uvicorn.
    """
    auth = auth_config or AuthConfig()

    # TaskStore writer (SPEC-056 Phase D, FR-7): a separate SqliteBackend
    # instance pointed at the SAME `store/arcui.db` file `app.state.observe`
    # reads — mutation routes never go through the read-side Observe plane.
    task_store_backend = SqliteBackend(
        (data_dir if data_dir is not None else resolve_data_dir()) / "store" / "arcui.db"
    )

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
        *agent_sessions_routes.routes,
        *agent_detail_routes.routes,
        *team_pages_routes.routes,
        *team_chat_routes.routes,
        *team_ws_routes.routes,
        *tasks_routes.routes,
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
        try:
            await task_store_backend.start()
        except Exception:  # reason: fail-open — dashboard still serves
            logger.exception("lifespan: task_store backend failed to start; writes will fail")
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
            # web_adapter is the core in-process adapter; embedded_gateway.adapters
            # holds every remote-platform adapter built by the plugin registry.
            for adapter in (embedded_gateway.web_adapter, *embedded_gateway.adapters):
                if adapter is not None:
                    await adapter.connect()
        # COMP-004 / REQ-090: when the deployment has a team but no service was
        # injected, construct the arcteam MessagingService over the same managed
        # NATS the bootstrap started. Without this the handle team_chat reads is
        # never set on a live deployment and channels silently read empty. Build
        # failures (broker down / no operator key) leave it None so the routes
        # surface an explicit service-unavailable error, never a fabricated [].
        resolved_service = messaging_service
        resolved_registry: Any | None = None
        built_backend: Any | None = None
        if resolved_service is None and team_root is not None:
            from arcui.messaging import build_messaging_service

            try:
                (
                    resolved_service,
                    resolved_registry,
                    built_backend,
                ) = await build_messaging_service()
            except Exception:  # reason: fail-open — dashboard must still serve
                logger.exception("lifespan: embedded messaging construction failed")
                resolved_service, resolved_registry, built_backend = None, None, None
        starlette_app.state.messaging_service = resolved_service
        # COMP-005: channel-management routes resolve agent refs to DIDs
        # through this registry. None when no service is wired — the mutation
        # routes then report the same explicit unavailable error as the reads.
        starlette_app.state.messaging_registry = resolved_registry
        # REQ-061: the ``/ws/team`` forwarder must be built HERE — the embedded
        # MessagingService only exists inside the lifespan. An operator group
        # post routes through it; the operator self-registers and auto-joins the
        # channel, then the message is signed and sent. Only build when the
        # deployment did not inject its own forwarder (tests) and a service is
        # live; otherwise the route degrades to a ``forward_unavailable`` frame.
        if team_post_forwarder is None and resolved_service is not None:
            from arcui.messaging import build_team_post_forwarder

            starlette_app.state.team_post_forwarder = build_team_post_forwarder(
                service=resolved_service,
                registry=resolved_registry,
            )
        # SPEC-031 F1: subscribe read-only to the arcteam bus and feed the
        # team-flow stream. Fail-open — a bus problem must never block the
        # dashboard; the stream just stays empty.
        observer_task: asyncio.Task[None] | None = None
        if resolved_service is not None:
            observer = TeamBusObserver(resolved_service, starlette_app.state.team_stream)
            observer_task = asyncio.create_task(
                observer.run(interval=team_stream_interval),
                name="arcui:team-bus-observer",
            )
        # Browser-open callback (registered by `arc ui start` on
        # loopback) hooks the same lifespan via app.router.lifespan
        # extension below.
        for hook in getattr(starlette_app.state, "_extra_startup_hooks", []):
            await hook()
        try:
            yield
        finally:
            if observer_task is not None:
                observer_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await observer_task
            # Close only a backend this lifespan opened — an injected service
            # (tests) owns its own backend lifecycle.
            if built_backend is not None:
                close = getattr(built_backend, "close", None)
                if close is not None:
                    try:
                        await close()
                    except Exception as exc:  # reason: fail-open — continue shutdown
                        # A NATS connection already tearing down raises
                        # ConnectionClosedError from drain() on every normal
                        # restart — expected, not traceback-worthy.
                        if type(exc).__name__ == "ConnectionClosedError":
                            logger.debug("lifespan: messaging backend already closed")
                        else:
                            logger.exception("lifespan: error closing embedded messaging backend")
            try:
                await starlette_app.state.observe.stop()
            except Exception:  # reason: fail-open — continue shutdown
                logger.exception("lifespan: error stopping arcstore Observe")
            try:
                await task_store_backend.stop()
            except Exception:  # reason: fail-open — continue shutdown
                logger.exception("lifespan: error stopping task_store backend")
            # SPEC-023: shut adapters down in reverse — disconnect cancels
            # all per-socket tasks and closes the WebSockets cleanly.
            if embedded_gateway is not None:
                for adapter in (embedded_gateway.web_adapter, *embedded_gateway.adapters):
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
    app.state.observe = Observe(data_dir=data_dir, workspace_dir=workspace_dir)
    # TaskStore writer (SPEC-056 Phase D) — see `task_store_backend` above.
    app.state.task_store = TaskStore(task_store_backend)
    # Ingest policy (ADR-019 tier = stringency): may operator-authored task
    # text carry URLs/emails? Federal → False (default, secure-by-default);
    # the launcher raises it for personal/enterprise. Reads are never gated.
    app.state.allow_external_task_refs = allow_external_task_refs
    app.state.config_controller = config_controller
    # arcteam MessagingService used by the Team Chat routes. ``None`` is
    # a supported state — the routes degrade to empty payloads so the
    # Team Chat tab never throws when the deployment lacks a team_root.
    app.state.messaging_service = messaging_service
    # COMP-005: registry for channel-membership ref→DID resolution. Set by the
    # lifespan when it builds the embedded service; None until then (and for
    # read-only test apps that inject only a service).
    app.state.messaging_registry = None
    # SPEC-031 C10: the read-only team-flow stream. The hub is always present
    # so ``/ws/team`` can accept viewers even before a bus is wired; it simply
    # stays quiet until the TeamBusObserver (started in lifespan) feeds it.
    app.state.team_stream = TeamStreamHub()
    # arcteam-owned forwarder for human group posts (REQ-061). arcui forwards,
    # never signs — see ``team_ws`` route.
    app.state.team_post_forwarder = team_post_forwarder
    app.state.agent_registry = agent_registry
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
