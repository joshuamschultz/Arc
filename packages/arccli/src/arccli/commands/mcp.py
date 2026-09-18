"""SPEC-082 T-1113 — ``arc mcp serve``: the surface that binds the MCP door.

arcagent is headless — it never binds a port. This command is the surface that
actually listens: it loads and starts an agent, assembles the door via the
arcagent facade, and serves it over one of two transports:

- ``--stdio`` (default): newline-delimited JSON-RPC over the process's own
  stdin/stdout — the transport a local MCP client (e.g. Claude Desktop) launches.
- ``--http --host H --port P``: the door's ASGI app under ``uvicorn``.

The arcagent door API is reached only through the ``import arcagent`` facade
(``arcagent.build_mcp_door`` / ``arcagent.serve_mcp_stdio``), keeping arccli off
arcagent's internals. Those are bound to the module-level names
``build_door_from_agent`` / ``serve_stdio`` (plus ``_load_arcagent`` /
``_process_streams`` / ``uvicorn``) so the command's wiring is testable without
starting an agent or binding a socket.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

import arcagent
import uvicorn

#: Per-line read cap for the stdio transport (mirrors the HTTP door's body cap):
#: the reader refuses a line over this size before buffering past it (LLM10).
_MAX_LINE_BYTES = 8 * 1024 * 1024

#: The door factory + stdio server, reached through the arcagent facade only.
build_door_from_agent = arcagent.build_mcp_door
serve_stdio = arcagent.serve_mcp_stdio


def _load_arcagent(agent: str) -> tuple[Any, Any, Path]:
    """Load a started-able ArcAgent for the agent directory ``agent``.

    Delegates to the shared agent loader (returns ``(ArcAgent, config, path)``);
    ``arcagent.build_mcp_door`` adapts that started agent to the door surface.
    """
    from arccli.commands.agent._common import _load_arcagent as _load_raw

    return _load_raw(Path(agent).expanduser())


def _process_streams() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Wire this process's stdin/stdout as asyncio JSON-RPC streams.

    The reader's byte ``limit`` is the per-line cap, so an unbounded line with no
    newline is refused before it is buffered past the cap (LLM10). Called on the
    idle serving loop (between ``run_until_complete`` steps) so the pipe
    connections complete synchronously.
    """
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader(limit=_MAX_LINE_BYTES)
    protocol = asyncio.StreamReaderProtocol(reader)
    loop.run_until_complete(loop.connect_read_pipe(lambda: protocol, sys.stdin))
    w_transport, w_protocol = loop.run_until_complete(
        loop.connect_write_pipe(asyncio.streams.FlowControlMixin, sys.stdout)
    )
    writer = asyncio.StreamWriter(w_transport, w_protocol, reader, loop)
    return reader, writer


def _serve_stdio(agent: Any) -> None:
    """Start the agent, build the door, and serve stdio until EOF."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(agent.startup())
        built = build_door_from_agent(agent)
        reader, writer = _process_streams()
        loop.run_until_complete(
            serve_stdio(built.router, reader=reader, writer=writer, max_line_bytes=_MAX_LINE_BYTES)
        )
    finally:
        loop.run_until_complete(agent.shutdown())
        loop.close()
        asyncio.set_event_loop(None)


def _serve_http(
    agent: Any,
    host: str,
    port: int,
    *,
    tls_cert: str | None = None,
    tls_key: str | None = None,
) -> None:
    """Start the agent, build the door, and serve its ASGI app under uvicorn.

    ``--tls-cert``/``--tls-key`` give the transport TLS. The door's own
    enterprise/federal mTLS gate reads the ASGI ``tls`` extension, which a
    TLS-terminating proxy in front of this app populates with the verified client
    certificate; without that extension a hardened-tier door fails closed (403).
    """
    ssl_kwargs: dict[str, Any] = {}
    if tls_cert and tls_key:
        ssl_kwargs = {"ssl_certfile": tls_cert, "ssl_keyfile": tls_key}
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(agent.startup())
        built = build_door_from_agent(agent)
        server = uvicorn.Server(
            uvicorn.Config(app=built.http_app, host=host, port=port, **ssl_kwargs)
        )
        loop.run_until_complete(server.serve())
    finally:
        loop.run_until_complete(agent.shutdown())
        loop.close()
        asyncio.set_event_loop(None)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc mcp serve",
        description="Serve this agent's MCP door over stdio (default) or HTTP.",
    )
    parser.add_argument("--agent", help="Agent directory to serve.")
    transport = parser.add_mutually_exclusive_group()
    transport.add_argument(
        "--stdio", action="store_true", help="Serve newline-delimited JSON-RPC over stdio."
    )
    transport.add_argument(
        "--http", action="store_true", help="Serve the door's ASGI app under uvicorn."
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host (--http only).")
    parser.add_argument("--port", type=int, default=8080, help="HTTP bind port (--http only).")
    parser.add_argument("--tls-cert", help="TLS certificate file for the HTTP transport (--http).")
    parser.add_argument("--tls-key", help="TLS private-key file for the HTTP transport (--http).")
    return parser


def mcp_handler(args: list[str]) -> None:
    """Handle ``arc mcp serve [...]``."""
    ns = _build_parser().parse_args(args)
    if not ns.agent:
        sys.stderr.write("arc mcp serve: --agent <dir> is required\n")
        sys.exit(1)

    agent, _config, _config_path = _load_arcagent(ns.agent)
    if ns.http:
        _serve_http(agent, ns.host, ns.port, tls_cert=ns.tls_cert, tls_key=ns.tls_key)
    else:
        _serve_stdio(agent)


__all__ = ["mcp_handler"]
