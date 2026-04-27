"""Plain CommandDef handlers for the `arc ui` subcommand group.

T1.1.5 migration: replaces the legacy Click-based dispatch in registry.py.
Each function is a direct translation of the corresponding Click command body
in arccli.ui, with Click-specific calls replaced with stdlib equivalents.

Layer contract: this module may import from arcui.
It MUST NOT import click or arccli.main_legacy.

Subcommands
-----------
arc ui start      — Launch the ArcUI dashboard server (standalone, no agent needed).
arc ui tail       — Connect to a running dashboard and stream events to stdout as JSONL.
                    Supports --layer, --agent, --group, and --viewer-token filters.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

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
    """Write agent token to well-known file for auto-discovery by agents."""
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(token)
    _TOKEN_FILE.chmod(0o600)


def _read_token_file() -> str | None:
    """Read persisted agent token. Returns None if not found."""
    try:
        return _TOKEN_FILE.read_text().strip() or None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Subcommand: start
# ---------------------------------------------------------------------------


def _start(args: argparse.Namespace) -> None:
    """Start the ArcUI dashboard server."""
    from arcui import create_app
    from arcui.auth import AuthConfig

    port: int = getattr(args, "port", 8420)
    host: str = getattr(args, "host", "127.0.0.1")
    viewer_token: str | None = getattr(args, "viewer_token", None)
    operator_token: str | None = getattr(args, "operator_token", None)
    agent_token: str | None = getattr(args, "agent_token", None)
    max_agents: int = getattr(args, "max_agents", 100)
    show_tokens: bool = getattr(args, "show_tokens", False)
    traces_dir: str | None = getattr(args, "traces_dir", None)

    config_dict: dict[str, str] = {}
    if viewer_token:
        config_dict["viewer_token"] = viewer_token
    if operator_token:
        config_dict["operator_token"] = operator_token
    if agent_token:
        config_dict["agent_token"] = agent_token

    auth = AuthConfig(config=config_dict) if config_dict else AuthConfig()

    _persist_agent_token(auth.agent_token)

    trace_store = None
    if traces_dir:
        traces_path = Path(traces_dir).resolve()
        if traces_path.exists():
            try:
                from arcllm.trace_store import JSONLTraceStore

                trace_store = JSONLTraceStore(traces_path)
                _write(f"  Trace store: {traces_path / 'traces'}")
            except ImportError:
                _write("  Warning: arcllm not installed, trace store disabled")
        else:
            _write(f"  Warning: traces-dir not found: {traces_path}")

    app = create_app(
        auth_config=auth,
        max_agents=max_agents,
        trace_store=trace_store,
    )

    fmt = str if show_tokens else _mask_token
    _write(f"ArcUI dashboard: http://{host}:{port}")
    _write(f"  Viewer token:   {fmt(app.state.auth_config.viewer_token)}")
    _write(f"  Operator token: {fmt(app.state.auth_config.operator_token)}")
    _write(f"  Agent token:    {fmt(app.state.auth_config.agent_token)}")
    _write(f"  Token file:     {_TOKEN_FILE}")
    _write(f"  Max agents:     {max_agents}")

    import uvicorn

    if trace_store is not None:
        import asyncio

        asyncio.run(app.state.aggregator.warm_start(trace_store))

    uvicorn.run(app, host=host, port=port, log_level="info")


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
                    sys.stderr.write(
                        f"arc ui tail: auth failed: {resp.get('error', resp)}\n"
                    )
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
        "--show-tokens", dest="show_tokens", action="store_true", default=False
    )
    p_start.add_argument("--traces-dir", dest="traces_dir", default=None)

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
