"""Plain CommandDef handlers for the `arc ui` subcommand group.

Subcommands
-----------
arc ui start      — Launch the ArcUI dashboard server. Reads operational history
                    on demand from the shared arcstore data dir (spool + WORM)
                    and on loopback bind opens the browser pre-authenticated.
arc ui tail       — Connect to a running dashboard and stream events to stdout as JSONL.
                    Supports --layer, --agent, --group, and --viewer-token filters.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path
from types import FrameType
from typing import Any, Protocol

from arcui._constants import BOOTSTRAP_HASH_KEY, LOOPBACK_HOSTS

from arccli.commands._shared import dispatch
from arccli.commands._shared import write as _write

# Valid layer values — enforced at parse time.
_VALID_LAYERS = ("llm", "run", "agent", "team")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _configure_logging(*, verbose: bool) -> None:
    """Configure process logging so audit events are actually observable (task #38).

    Live bug: nothing in `arc ui start` ever configured logging. Python's
    interpreter default — root logger effectively WARNING, no handler —
    silently dropped every INFO record: UIAuditLogger's ``ui.mutation`` /
    ``ui.session_start`` events (arcui.audit), and every platform-adapter
    connect/auth-reject line. Verified live: zero audit lines in journald
    despite mutations genuinely happening. ``uvicorn.Config(log_level="info")``
    only configures uvicorn's OWN loggers ("uvicorn"/"uvicorn.access") — it
    has no effect on these.

    Root stays at WARNING (third-party library chatter stays quiet); the
    audit logger and platform-adapter loggers are explicitly raised to INFO
    — the two categories confirmed silent in production. Adapter packages
    install as top-level modules with underscores (``arcgateway_telegram``,
    not ``arcgateway.telegram``), so each needs its own entry — setting
    "arcgateway" alone would not cover them.

    ``force=True`` guarantees this config wins even if something already
    called ``basicConfig`` first (a dependency, or — in tests — a prior
    call in the same process) — `arc ui start` is a process entrypoint and
    owns logging config for the whole process.

    ``verbose`` raises the root logger itself to INFO, matching the
    existing ``--verbose``/``-v`` convention ``arc agent serve`` uses.
    """
    import logging

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    logging.getLogger("arcui.audit").setLevel(logging.INFO)
    logging.getLogger("arcgateway.adapters").setLevel(logging.INFO)
    logging.getLogger("arcgateway_telegram").setLevel(logging.INFO)
    logging.getLogger("arcgateway_slack").setLevel(logging.INFO)
    logging.getLogger("arcgateway_mattermost").setLevel(logging.INFO)
    if verbose:
        logging.getLogger().setLevel(logging.INFO)


def _mask_token(token: str) -> str:
    """Mask a token for display: show first 8 and last 8 chars."""
    if len(token) <= 16:
        return "****"
    return f"{token[:8]}...{token[-8:]}"


# ---------------------------------------------------------------------------
# Zero-config helpers
# ---------------------------------------------------------------------------


def _maybe_build_gateway_config(args: argparse.Namespace, team_root: Path | None) -> Any | None:
    """Build a GatewayConfig for the in-process gateway runtime (SPEC-023).

    Resolution order:
      1. ``--no-chat`` ⇒ return None (chat disabled).
      2. ``--gateway-config <path>`` ⇒ load from TOML.
      3. ``team_root`` is set ⇒ build a default with [platforms.web]
         enabled and the same defaults the wizard produces.
      4. Otherwise return None — no chat, but the dashboard still works.
    """
    if getattr(args, "no_chat", False):
        return None

    from arcgateway.config import GatewayConfig

    config_path: str | None = getattr(args, "gateway_config", None)
    if config_path:
        return GatewayConfig.from_toml(Path(config_path).expanduser().resolve())

    if team_root is None:
        return None

    return GatewayConfig.from_toml_str(
        '[gateway]\nagent_did = "did:arc:agent:default"\n\n[platforms.web]\nenabled = true\n'
    )


class BrowserLauncher(Protocol):
    """How `arc ui start` opens the dashboard.

    Default implementation calls `webbrowser.open` against a loopback
    URL with the viewer token in the hash. Stub/kiosk/Electron variants
    can replace this without touching `_start`. Wave 2 TD-MED:
    decouples the launcher from the only-mechanism webbrowser
    dependency and gives federal kiosk deployments a plug-in seam.
    """

    def open_dashboard(self, host: str, port: int, viewer_token: str) -> bool:
        """Open the dashboard pre-authenticated; return True iff opened."""
        ...


class DefaultBrowserLauncher:
    """Production launcher: webbrowser.open with loopback gating + SR-4.

    Only fires on loopback bind addresses; non-loopback binds are
    intentionally silent so the URL containing the viewer token never
    reaches a remote machine or log shipper. On failure, the caller
    falls back via `_print_browser_open_fallback` — review finding C-2:
    this method MUST NOT print the URL+token combination to stdout.
    """

    def open_dashboard(self, host: str, port: int, viewer_token: str) -> bool:
        if host not in LOOPBACK_HOSTS:
            return False
        import webbrowser

        url = f"http://{host}:{port}/#{BOOTSTRAP_HASH_KEY}={viewer_token}"
        try:
            return webbrowser.open(url)
        except (webbrowser.Error, OSError):
            return False


def _maybe_open_browser(host: str, port: int, viewer_token: str) -> bool:
    """Backward-compatible function form of `DefaultBrowserLauncher`.

    Tests still patch this symbol; keeping it as a thin wrapper lets us
    introduce the Protocol without breaking the existing test surface.
    The launcher used by `_start` is the Protocol-typed instance, not
    this function — so a stub launcher in tests fully bypasses
    `webbrowser.open` without needing to patch this symbol.
    """
    return DefaultBrowserLauncher().open_dashboard(host, port, viewer_token)


def _magic_link(host: str, port: int, viewer_token: str) -> str:
    """Build the one-click, pre-authenticated dashboard URL.

    The viewer token rides in the URL *hash* (`#auth=<token>`), which the
    browser never sends to the server — so it cannot land in uvicorn access
    logs or a Referer header. The SPA's `bootstrapAuth()` consumes it on
    load, persists it to localStorage, and strips the fragment.
    """
    return f"http://{host}:{port}/#{BOOTSTRAP_HASH_KEY}={viewer_token}"


def _print_browser_open_fallback(host: str, port: int) -> None:
    """Point the operator at the magic-link when `webbrowser.open` failed.

    Only reached on a loopback bind (the auto-open hook is registered
    nowhere else), where `_start` has already printed the full magic-link
    and token above. So this is a one-line nudge — the link is already on
    screen for copy-paste.
    """
    _write(
        f"  Browser did not auto-open — copy the link above into a browser "
        f"to reach http://{host}:{port}/ pre-authenticated."
    )


# ---------------------------------------------------------------------------
# Subcommand: start
# ---------------------------------------------------------------------------


def _start(args: argparse.Namespace) -> None:
    """Start the ArcUI dashboard server.

    The dashboard reads operational history on demand from the shared arcstore
    data dir (the Observe plane), so there is no per-agent trace-store discovery
    here. On loopback bind it opens the browser pre-authenticated; non-loopback
    prints tokens for manual paste with a security warning.
    """
    from arcui import create_app
    from arcui.auth import AuthConfig

    from arccli.commands.agent import _load_env

    # Configure logging FIRST — before anything else can emit a log record —
    # so audit events and adapter connect lines are observable (task #38).
    _configure_logging(verbose=getattr(args, "verbose", False))

    # Load the deployment's .env (cwd + ${ARC_CONFIG_DIR:-~/.arc} + ~) BEFORE
    # building agents, so their provider keys resolve without a manual export.
    _load_env()

    port: int = getattr(args, "port", 8420)
    host: str = getattr(args, "host", "127.0.0.1")
    viewer_token: str | None = getattr(args, "viewer_token", None)
    operator_token: str | None = getattr(args, "operator_token", None)
    max_agents: int = getattr(args, "max_agents", 100)
    show_tokens: bool = getattr(args, "show_tokens", False)
    no_browser: bool = getattr(args, "no_browser", False)

    config_dict: dict[str, str] = {}
    if viewer_token:
        config_dict["viewer_token"] = viewer_token
    if operator_token:
        config_dict["operator_token"] = operator_token

    auth = AuthConfig(config=config_dict) if config_dict else AuthConfig()

    team_root_arg: str | None = getattr(args, "team_root", None)
    team_root: Path | None
    if team_root_arg:
        team_root = Path(team_root_arg).expanduser().resolve()
    else:
        default = Path.cwd() / "team"
        team_root = default.resolve() if default.is_dir() else None

    # SPEC-023: when a team_root is present, auto-build a default
    # GatewayConfig so the in-process web platform is wired and
    # /ws/chat/{agent_id} works out of the box. Operators who need a
    # custom config (slack/telegram, federal tier) can override via
    # --gateway-config later.
    gateway_config = _maybe_build_gateway_config(args, team_root)

    app = create_app(
        auth_config=auth,
        max_agents=max_agents,
        team_root=team_root,
        gateway_config=gateway_config,
    )

    is_loopback = host in LOOPBACK_HOSTS
    viewer_token_value = app.state.auth_config.viewer_token
    operator_token_value = app.state.auth_config.operator_token

    if is_loopback:
        # Personal/local-dev: the whole point of `arc ui start` is a working
        # one-click link. Print the full magic-link (token in the URL hash,
        # never sent to the server) plus the bare token for the sign-in field
        # fallback. The operator token stays masked unless --show-tokens —
        # it grants control, and viewing the dashboard only needs the viewer.
        _write("ArcUI dashboard is running. Open this link (already signed in):")
        _write(f"    {_magic_link(host, port, viewer_token_value)}")
        _write("")
        _write("  Or open the dashboard and paste this token into the sign-in field:")
        _write(f"    Dashboard:      http://{host}:{port}")
        _write(f"    Viewer token:   {viewer_token_value}")
        op_display = operator_token_value if show_tokens else _mask_token(operator_token_value)
        _write(f"    Operator token: {op_display}")
        _write(f"    Max agents:     {max_agents}")
    else:
        # Non-loopback: the link/token could reach a remote log shipper, so
        # keep the strict masked-unless-`--show-tokens` posture and never
        # emit the token inside a URL (review C-2).
        fmt = str if show_tokens else _mask_token
        _write(f"ArcUI dashboard: http://{host}:{port}")
        _write(f"  Viewer token:   {fmt(viewer_token_value)}")
        _write(f"  Operator token: {fmt(operator_token_value)}")
        _write(f"  Max agents:     {max_agents}")
        _write(
            "  WARNING: bound to a non-loopback address. "
            "Tokens above are required for browser access; copy the viewer "
            "token and paste it into the dashboard's auth field."
        )

    import asyncio

    import uvicorn

    if is_loopback and not no_browser:
        # SPEC-019 T5.3: mark the bootstrapped token so AuthMiddleware emits
        # `ui.session_start` with auth_method="browser_bootstrap" on first
        # request from this token.
        tracker = getattr(app.state, "session_tracker", None)
        if tracker is not None:
            tracker.mark_bootstrap_issued(viewer_token_value)

        # Hook into Starlette's lifespan via the extra-startup-hooks
        # list set up by `create_app` (Wave 2 TD-04). Avoids the
        # deprecated `on_startup=` parameter while keeping the same
        # "browser opens after server reports ready" guarantee.
        async def _open_browser_on_ready() -> None:
            opened = _maybe_open_browser(host, port, viewer_token_value)
            if not opened:
                _print_browser_open_fallback(host, port)

        app.state._extra_startup_hooks.append(_open_browser_on_ready)

    # When a team_root is set, auto-start the messaging infra (NATS JetStream +
    # agent registration) before serving, so the fleet works out of the box.
    # The broker is spawned in its own bootstrap loop and reaped by PID after
    # the (blocking) server returns — loop-independent, so uvicorn's own signal
    # handling can never orphan it.
    infra = None
    fleet = None
    if team_root is not None:
        from arccli.commands._serve import bootstrap_infra

        infra = asyncio.run(bootstrap_infra(team_root))
        if infra is not None:
            _install_infra_reaper(infra)

        # MSG4: start every team agent up front so its messaging inbox loop runs
        # and the fleet RESPONDS to DMs/@mentions/channel posts — not just the
        # agents someone happens to web-chat. The gateway factory shares these
        # same instances, so there is one durable consumer per agent.
        if _fleet_enabled(gateway_config):
            fleet = _register_fleet_startup(app, team_root)

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    try:
        uvicorn.Server(config).run()
    finally:
        if fleet is not None:
            from arcgateway.fleet import set_current_fleet

            set_current_fleet(None)
        if infra is not None:
            infra.terminate_sync()


def _fleet_enabled(gateway_config: Any | None) -> bool:
    """Whether to run the in-process always-on fleet for this deployment.

    Runs for personal/enterprise (the in-process AsyncioExecutor path). Skipped
    only at federal tier, where the gateway isolates each session in its own
    SubprocessExecutor — an in-process shared fleet would violate that isolation
    and does not share the factory the subprocess model uses. Federal always-on
    is a separate (per-agent daemon) design, out of scope here.
    """
    if gateway_config is None:
        return True  # --no-chat: agents still consume; no gateway to conflict with
    return str(getattr(getattr(gateway_config, "gateway", None), "tier", "personal")) != "federal"


def _register_fleet_startup(app: Any, team_root: Path) -> Any:
    """Wire the always-on fleet: start every team agent so its inbox loop runs.

    Builds a process :class:`~arcgateway.fleet.FleetRegistry`, installs it so the
    embedded gateway factory reuses its instances (one durable consumer per
    agent), and appends a lifespan startup hook that starts the fleet AFTER the
    broker + gateway are up. Returns the fleet so the caller can clear it on
    shutdown. arcui owns none of this — it uses the public ``_extra_startup_hooks``
    seam and ``app.state.executor``.
    """
    from arcgateway.fleet import FleetRegistry, set_current_fleet

    fleet = FleetRegistry()
    set_current_fleet(fleet)
    # Expose the fleet handle so a future dashboard start/stop control (owned by
    # arcui) can drive per-agent lifecycle — see the MSG4 report's contract.
    app.state.fleet = fleet

    async def _serve_fleet() -> None:
        from arccli.commands._serve import serve_fleet_agents

        executor = getattr(app.state, "executor", None)

        async def _warm(agent_did: str) -> None:
            factory = getattr(executor, "agent_factory", None) if executor is not None else None
            if factory is not None:
                await factory(agent_did)

        count = await serve_fleet_agents(team_root, fleet, warm=_warm)
        _write(f"  Fleet: {count} always-on agent(s) started (messaging inbox active).")

    app.state._extra_startup_hooks.append(_serve_fleet)
    return fleet


def _install_infra_reaper(infra: Any) -> None:
    """Reap the managed NATS broker on SIGTERM.

    uvicorn's ``.run()`` returns cleanly on SIGINT (Ctrl-C) and normal exit — the
    ``finally`` reaps the broker there. A raw SIGTERM does not trigger uvicorn's
    clean shutdown on every platform, so install a process handler that
    terminates the broker before re-raising the default action; the child broker
    never outlives the dashboard.
    """
    import signal

    def _on_sigterm(signum: int, _frame: FrameType | None) -> None:
        infra.terminate_sync()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    with contextlib.suppress(ValueError):  # not main thread → uvicorn handles it
        signal.signal(signal.SIGTERM, _on_sigterm)


# ---------------------------------------------------------------------------
# Subcommand: tail
# ---------------------------------------------------------------------------


def _build_subscribe_message(args: argparse.Namespace) -> dict[str, Any]:
    """Build the WebSocket subscribe message from parsed args.

    Returns a dict ready to send as JSON over the /ws WebSocket.
    Only includes filter keys that are set (non-None, non-empty).
    """
    msg: dict[str, Any] = {"type": "subscribe"}

    layer: str | None = getattr(args, "layer", None)
    agent: str | None = getattr(args, "agent", None)
    group: str | None = getattr(args, "group", None)

    if layer:
        msg["layers"] = [layer]
    if agent:
        msg["agents"] = [agent]
    if group:
        msg["teams"] = [group]

    return msg


def _import_ws_connect() -> Any | None:
    """Import the websockets async connect context manager.

    Returns None if websockets is not installed.
    Supports both the legacy API (websockets.connect) and the modern API
    (websockets.asyncio.client.connect introduced in websockets >= 13).
    """
    import importlib

    # Try modern API first (websockets >= 13)
    for module_path in ("websockets.asyncio.client", "websockets.client"):
        try:
            mod = importlib.import_module(module_path)
            connect = getattr(mod, "connect", None)
            if connect is not None:
                return connect
        except ImportError:
            continue
    return None


def _tail(args: argparse.Namespace) -> None:
    """Connect to ArcUI and stream events to stdout as JSONL.

    Reads events from the /ws WebSocket endpoint, applies server-side
    subscription filters, and prints each event as a JSON line to stdout.

    Ctrl-C terminates cleanly.
    """
    import asyncio

    port: int = getattr(args, "port", 8420)
    host: str = getattr(args, "host", "127.0.0.1")
    viewer_token: str | None = getattr(args, "viewer_token", None)

    if not viewer_token:
        sys.stderr.write(
            "arc ui tail: no --viewer-token supplied.\n"
            "  Provide one with --viewer-token <token> or start the server\n"
            "  with --viewer-token and pass the same value here.\n"
        )
        sys.exit(1)

    subscribe_msg = _build_subscribe_message(args)

    async def _run() -> None:
        # Import the websockets connect function, supporting both API shapes.
        # websockets >= 12 moved the async API to websockets.asyncio.client.
        ws_connect = _import_ws_connect()
        if ws_connect is None:
            sys.stderr.write(
                "arc ui tail requires the 'websockets' package.\n"
                "  Install with: pip install websockets\n"
            )
            sys.exit(1)

        uri = f"ws://{host}:{port}/ws"
        sys.stderr.write(f"Connecting to {uri} ...\n")

        try:
            async with ws_connect(uri) as ws:
                # First-message auth
                await ws.send(json.dumps({"token": viewer_token}))
                raw = await ws.recv()
                resp = json.loads(raw)
                if resp.get("type") != "auth_ok":
                    sys.stderr.write(f"arc ui tail: auth failed: {resp.get('error', resp)}\n")
                    sys.exit(1)

                sys.stderr.write(
                    f"Connected as {resp.get('role', 'viewer')}. Streaming events...\n"
                )

                # Send subscription filter
                await ws.send(json.dumps(subscribe_msg))

                # Stream events to stdout as JSONL
                async for message in ws:
                    if isinstance(message, str):
                        sys.stdout.write(message + "\n")
                        sys.stdout.flush()

        except (ConnectionRefusedError, OSError) as exc:
            sys.stderr.write(
                f"arc ui tail: cannot connect to {uri}: {exc}\n"
                "  Is the dashboard running? Start it with: arc ui start\n"
            )
            sys.exit(1)
        except KeyboardInterrupt:
            sys.stderr.write("\narc ui tail: disconnected.\n")

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# Argparse-based dispatcher
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for `arc ui <sub> [args]`."""
    parser = argparse.ArgumentParser(
        prog="arc ui",
        description="ArcUI dashboard server.",
        add_help=True,
    )
    subs = parser.add_subparsers(dest="subcmd", metavar="<subcommand>")

    # ── start ──────────────────────────────────────────────────────────
    p_start = subs.add_parser(
        "start",
        help="Start the ArcUI dashboard server.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p_start.add_argument("--port", type=int, default=8420, help="Server port.")
    p_start.add_argument("--host", default="127.0.0.1", help="Bind address.")
    p_start.add_argument("--viewer-token", dest="viewer_token", default=None)
    p_start.add_argument("--operator-token", dest="operator_token", default=None)
    p_start.add_argument("--max-agents", dest="max_agents", type=int, default=100)
    p_start.add_argument(
        "--team-root",
        dest="team_root",
        default=None,
        help=(
            "Directory containing <name>_agent/ subdirs. SPEC-022 routes "
            "(/api/team/roster, /api/agents/{id}/...) discover agents from "
            "here. Defaults to ./team if it exists, else None."
        ),
    )
    p_start.add_argument("--show-tokens", dest="show_tokens", action="store_true", default=False)
    p_start.add_argument(
        "--no-browser",
        dest="no_browser",
        action="store_true",
        default=False,
        help="Do not auto-open a browser tab on loopback start. Useful for tests, "
        "headless boxes, and developers who already have a tab open.",
    )
    p_start.add_argument(
        "--gateway-config",
        dest="gateway_config",
        default=None,
        help="Path to gateway.toml. When omitted, a default config is "
        "auto-built with [platforms.web].enabled=true so /ws/chat/{agent_id} "
        "works out of the box. Pass an explicit file to enable Slack/Telegram "
        "or set tier=federal.",
    )
    p_start.add_argument(
        "--no-chat",
        dest="no_chat",
        action="store_true",
        default=False,
        help="Disable the in-process web chat platform even when team_root is set.",
    )
    p_start.add_argument(
        "--verbose",
        "-v",
        dest="verbose",
        action="store_true",
        default=False,
        help="Raise the root logger to INFO (default: only arcui.audit + "
        "platform-adapter loggers are INFO; everything else stays WARNING).",
    )

    # ── tail ───────────────────────────────────────────────────────────
    p_tail = subs.add_parser(
        "tail",
        help="Connect to a running ArcUI server and stream events to stdout.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p_tail.add_argument(
        "--host",
        default="127.0.0.1",
        help="ArcUI server host.",
    )
    p_tail.add_argument(
        "--port",
        type=int,
        default=8420,
        help="ArcUI server port.",
    )
    p_tail.add_argument(
        "--viewer-token",
        dest="viewer_token",
        default=None,
        help="Viewer token for authentication (required).",
    )
    p_tail.add_argument(
        "--layer",
        choices=_VALID_LAYERS,
        default=None,
        help="Filter events to a specific layer: llm, run, agent, or team.",
    )
    p_tail.add_argument(
        "--agent",
        default=None,
        metavar="AGENT_ID",
        help="Filter events to a specific agent (by agent_id or DID).",
    )
    p_tail.add_argument(
        "--group",
        default=None,
        metavar="GROUP_NAME",
        help="Filter events to agents in a specific team or group.",
    )

    return parser


_SUBCOMMAND_MAP = {
    "start": _start,
    "tail": _tail,
}


def ui_handler(args: list[str]) -> None:
    """Top-level handler for `arc ui <sub> [args]`.

    Called by arccli.commands.registry when the user runs `arc ui ...`.
    """
    dispatch(_build_parser(), _SUBCOMMAND_MAP, args)
