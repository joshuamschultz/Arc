"""SPEC-082 T-1111 / SPEC-084 T-1146 — the stdio transport, served by the ``mcp`` SDK.

The transport a local MCP client (e.g. Claude Desktop) launches: newline-delimited
JSON-RPC over the process's stdin/stdout. It is now served by the official ``mcp``
SDK stdio server driving the door's low-level :class:`~mcp.server.lowlevel.Server`, so
``arc mcp serve --stdio`` speaks the real MCP handshake — the same ``Server`` object
the HTTP transport serves.

It is transport-only: it wires the SDK stdio streams to ``Server.run`` and does no
policy of its own (identity, allowlist, dispatch and audit live in the SDK server's
handlers). ``stdin`` / ``stdout`` default to the real process streams; tests inject
their own to drive a handshake without touching the process handles.
"""

from __future__ import annotations

from typing import Any

import anyio
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


async def serve_stdio(
    server: Server[Any, Any],
    *,
    stdin: anyio.AsyncFile[str] | None = None,
    stdout: anyio.AsyncFile[str] | None = None,
) -> None:
    """Serve ``server`` over newline-delimited JSON-RPC on stdio until EOF.

    Drives the SDK stdio transport: it reads JSON-RPC messages from ``stdin``,
    dispatches each through the server's handlers, and writes responses to
    ``stdout``. Returns when the input stream reaches EOF.
    """
    async with stdio_server(stdin=stdin, stdout=stdout) as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


__all__ = ["serve_stdio"]
