"""Plain CommandDef handlers for the `arc ui` subcommand group.

Subcommands
-----------
arc ui start      — Launch the ArcUI dashboard server.
                    Discovers all registered agents via the arcteam registry,
                    warm-starts the aggregator from each agent's workspace, and
                    on loopback bind opens the browser pre-authenticated.
arc ui tail       — Connect to a running dashboard and stream events to stdout as JSONL.
                    Supports --layer, --agent, --group, and --viewer-token filters.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Protocol

from arcui._constants import BOOTSTRAP_HASH_KEY, LOOPBACK_HOSTS

_TOKEN_FILE = Path.home() / ".arcagent" / "ui-token"

# Valid layer values — enforced at parse time.
_VALID_LAYERS = ("llm", "run", "agent", "team")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _write(msg: str = "") -> None:
    """Write a line to stdout."""
    sys.stdout.write(msg + "\n")


def _mask_token(token: str) -> str:
    """Mask a token for display: show first 8 and last 8 chars."""
    if len(token) <= 16:
        return "****"
    return f"{token[:8]}...{token[-8:]}"


def _persist_agent_token(token: str) -> None:
    """Atomically write the agent token with 0600 from creation.

    Delegates to `arcagent.utils.secure_file.write_secret` (Wave 2
    TD-MED). Future credential writers (vault cache, federated peer
    keys) inherit the same SR-1 posture — atomic 0600, parent 0700,
    no umask race — instead of reinventing it inline.
    """
    from arcagent.utils.secure_file import write_secret

    write_secret(_TOKEN_FILE, token)


def _read_token_file() -> str | None:
    """Read persisted agent token. Returns None if not found."""
    try:
        return _TOKEN_FILE.read_text().strip() or None
    except OSError:
        return None


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
        '[gateway]\n'
        'agent_did = "did:arc:agent:default"\n'
        '\n'
        '[platforms.web]\n'
        'enabled = true\n'
    )


def _resolve_trace_stores(args: argparse.Namespace) -> list[Any]:
    """Build the list of TraceStores from the arcteam registry.

    Composes `_load_entities` (registry I/O) and `_entities_to_stores`
    (path validation + JSONLTraceStore construction). Wave 2 split:
    each helper has one job and is independently testable.
    """
    entities = _load_entities(args)
    return _entities_to_stores(entities)


def _load_entities(args: argparse.Namespace) -> list[Any]:
    """Read every Entity from the arcteam registry (read-only).

    Wave 2 simplification: uses `arcteam.registry.list_entities_readonly`
    so the CLI doesn't bootstrap an `AuditLogger` (HMAC key load,
    initialize, etc.) just to enumerate agents. The audit chain is for
    *write* paths; a read-only launcher doesn't belong on it.

    Returns [] on registry I/O failure (with a warning) so the caller
    can launch in zero-store mode rather than abort.
    """
    import asyncio

    from arcteam.config import TeamConfig
    from arcteam.registry import list_entities_readonly
    from arcteam.storage import FileBackend

    # Resolution order:
    #   1. explicit --root             (test harnesses, advanced users)
    #   2. <--team-root>/shared        (production: arc-stack.sh passes
    #                                    --team-root <repo>/team and the
    #                                    team backend lives at /shared per
    #                                    `arc team init` convention)
    #   3. TeamConfig().root default   (~/.arc/team/)
    # Without step 2, `arc ui start --team-root .../team` would silently
    # read trace-store entities from ~/.arc/team/ — empty in production —
    # and every /api/agents/{id}/traces call would return [].
    root_arg: str | None = getattr(args, "root", None)
    team_root_arg: str | None = getattr(args, "team_root", None)
    if root_arg:
        root = Path(root_arg)
    elif team_root_arg:
        root = Path(team_root_arg) / "shared"
    else:
        root = TeamConfig().root
    backend = FileBackend(root)

    try:
        return asyncio.run(list_entities_readonly(backend))
    except (FileNotFoundError, OSError) as exc:
        _write(f"  registry unavailable: {exc}")
        return []


def _entities_to_stores(entities: list[Any]) -> list[Any]:
    """Build one JSONLTraceStore per agent with a valid workspace_path.

    Entities without `workspace_path` or whose stored path no longer
    exists are skipped with a warning. Pure transform; no I/O outside
    the path-existence check.
    """
    from arcllm.trace_store import JSONLTraceStore

    stores: list[Any] = []
    for entity in entities:
        if entity.workspace_path is None:
            _write(f"  skip {entity.id}: no workspace_path (run `arc team backfill-workspaces`)")
            continue
        wp = Path(entity.workspace_path)
        if not wp.is_dir():
            _write(f"  skip {entity.id}: workspace_path {wp} not found")
            continue
        # JSONLTraceStore takes the agent root; workspace_path points at the
        # agent's workspace subdir (per `arc agent create` convention).
        stores.append(JSONLTraceStore(wp.parent))
    return stores


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


def _print_browser_open_fallback(
    host: str,
    port: int,
    viewer_token: str,
    *,
    show_tokens: bool,
) -> None:
    """Print bootstrap instructions when `webbrowser.open` could not run.

    Splits the URL and the token across two lines so neither carries the
    other; the URL is safe to log, the token follows the same
    masked-unless-`--show-tokens` rule as the rest of the startup banner.
    No `#auth=...` fragment is ever emitted from this path (review C-2).
    """
    fmt = str if show_tokens else _mask_token
    _write(
        "  Browser did not auto-open. Visit the URL below and paste the "
        "viewer token into the auth field:"
    )
    _write(f"    URL:          http://{host}:{port}/")
    _write(f"    Viewer token: {fmt(viewer_token)}")


# ---------------------------------------------------------------------------
# Subcommand: start
# ---------------------------------------------------------------------------


def _start(args: argparse.Namespace) -> None:
    """Start the ArcUI dashboard server.

    Discovers all registered agents via the arcteam registry, warm-starts
    the aggregator from each agent's workspace, and on loopback bind opens
    the browser pre-authenticated. Non-loopback prints tokens for manual
    paste with a security warning.
    """
    from arcui import create_app
    from arcui.auth import AuthConfig
    from arcui.federated_store import FederatedTraceStore

    port: int = getattr(args, "port", 8420)
    host: str = getattr(args, "host", "127.0.0.1")
    viewer_token: str | None = getattr(args, "viewer_token", None)
    operator_token: str | None = getattr(args, "operator_token", None)
    agent_token: str | None = getattr(args, "agent_token", None)
    max_agents: int = getattr(args, "max_agents", 100)
    show_tokens: bool = getattr(args, "show_tokens", False)
    no_browser: bool = getattr(args, "no_browser", False)

    config_dict: dict[str, str] = {}
    if viewer_token:
        config_dict["viewer_token"] = viewer_token
    if operator_token:
        config_dict["operator_token"] = operator_token
    if agent_token:
        config_dict["agent_token"] = agent_token

    auth = AuthConfig(config=config_dict) if config_dict else AuthConfig()

    _persist_agent_token(auth.agent_token)

    stores = _resolve_trace_stores(args)
    # Wrap discovered stores in a federation. Zero stores → no trace
    # backing for `/api/traces`; ≥1 stores → FederatedTraceStore so the
    # routes see a uniform read surface regardless of how many workspaces
    # are mounted (Wave 2 simplification: was a one-line helper).
    trace_store = FederatedTraceStore(stores) if stores else None

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
        trace_store=trace_store,
        team_root=team_root,
        gateway_config=gateway_config,
    )

    is_loopback = host in LOOPBACK_HOSTS
    # SR-4: tokens are masked unless --show-tokens; loopback never needs them
    # (browser auto-bootstraps), so do not auto-flip show.
    fmt = str if show_tokens else _mask_token
    _write(f"ArcUI dashboard: http://{host}:{port}")
    _write(f"  Viewer token:   {fmt(app.state.auth_config.viewer_token)}")
    _write(f"  Operator token: {fmt(app.state.auth_config.operator_token)}")
    _write(f"  Agent token:    {fmt(app.state.auth_config.agent_token)}")
    _write(f"  Token file:     {_TOKEN_FILE}")
    _write(f"  Max agents:     {max_agents}")
    _write(f"  Trace stores:   {len(stores)}")

    if not is_loopback:
        _write(
            "  WARNING: bound to a non-loopback address. "
            "Tokens above are required for browser access; copy the viewer "
            "token and paste it into the dashboard's auth field."
        )

    import asyncio

    import uvicorn

    if trace_store is not None and stores:
        asyncio.run(app.state.aggregator.warm_start_multi(stores))

    if is_loopback and not no_browser:
        viewer_token_value = app.state.auth_config.viewer_token

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
                _print_browser_open_fallback(
                    host, port, viewer_token_value, show_tokens=show_tokens
                )

        app.state._extra_startup_hooks.append(_open_browser_on_ready)

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    uvicorn.Server(config).run()


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

    # Auto-discover viewer token from persisted token file if not supplied.
    # The persisted file contains the agent token, so we cannot use it for
    # /ws (agent tokens are rejected there). Only use it if explicitly set.
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
    p_start.add_argument("--agent-token", dest="agent_token", default=None)
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
    parser = _build_parser()

    if not args:
        parser.print_help()
        sys.exit(0)

    parsed = parser.parse_args(args)

    if parsed.subcmd is None:
        parser.print_help()
        sys.exit(0)

    fn = _SUBCOMMAND_MAP.get(parsed.subcmd)
    if fn is None:
        sys.stderr.write(f"arc ui: unknown subcommand '{parsed.subcmd}'\n")
        sys.exit(1)

    fn(parsed)
