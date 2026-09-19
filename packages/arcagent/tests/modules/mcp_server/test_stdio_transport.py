"""SPEC-082 T-1111 / SPEC-084 T-1146 — the stdio transport, served by the ``mcp`` SDK.

``arc mcp serve --stdio`` serves the door's ``mcp`` SDK server over newline-delimited
JSON-RPC on the process's stdin/stdout. ``serve_stdio(server, *, stdin, stdout)`` wires
the SDK stdio transport to ``Server.run``; ``stdin``/``stdout`` default to the real
process streams and are injected here so a real handshake can be driven over in-process
OS pipes — no subprocess, no touching the process handles.

The tests drive the genuine wire: a real ``initialize`` → ``tools/list`` exchange over
the pipes returns the allowlist-filtered catalog, and an immediately-closed stdin ends
the serve loop cleanly at EOF.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import anyio
import pytest
from mcp.types import LATEST_PROTOCOL_VERSION

from arcagent.modules.mcp_server.allowlist import ExposureAllowlist
from arcagent.modules.mcp_server.config import McpServerConfig
from arcagent.modules.mcp_server.sdk_server import build_sdk_server
from arcagent.modules.mcp_server.server import McpServer
from arcagent.modules.mcp_server.stdio_transport import serve_stdio

_TOOL = "echo"


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"the {name} tool"
        self.input_schema: dict[str, Any] = {"type": "object", "properties": {}}


class _FakeRegistry:
    def __init__(self, tools: dict[str, _FakeTool]) -> None:
        self.tools = tools


def _door_server() -> Any:
    """The door's SDK server over one exposed tool, listing-only (personal tier)."""
    mcp_server = McpServer(_FakeRegistry({_TOOL: _FakeTool(_TOOL)}))  # type: ignore[arg-type]
    allowlist = ExposureAllowlist.from_config(
        McpServerConfig(enabled=True, expose=[_TOOL]), tier="personal"
    )
    return build_sdk_server(mcp_server, tier="personal", allowlist=allowlist)


def _line(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload) + "\n").encode("utf-8")


@pytest.mark.asyncio
async def test_real_handshake_over_stdio_lists_the_catalog() -> None:
    """A real ``initialize`` → ``tools/list`` over stdio returns the exposed catalog."""
    client_to_server_r, client_to_server_w = os.pipe()
    server_to_client_r, server_to_client_w = os.pipe()

    server_stdin = anyio.wrap_file(os.fdopen(client_to_server_r, "r", encoding="utf-8"))
    server_stdout = anyio.wrap_file(os.fdopen(server_to_client_w, "w", encoding="utf-8"))

    # Read the server's stdout lines through an asyncio StreamReader on the pipe.
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    read_file = os.fdopen(server_to_client_r, "rb", buffering=0)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), read_file)

    serve_task = asyncio.create_task(
        serve_stdio(_door_server(), stdin=server_stdin, stdout=server_stdout)
    )
    try:
        # A real client handshake: initialize, then the initialized notification.
        os.write(
            client_to_server_w,
            _line(
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": LATEST_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"},
                    },
                }
            ),
        )
        init_line = await asyncio.wait_for(reader.readline(), timeout=5)
        assert json.loads(init_line)["id"] == 0, "no initialize result over stdio"

        os.write(client_to_server_w, _line({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        os.write(
            client_to_server_w,
            _line({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}),
        )
        list_line = await asyncio.wait_for(reader.readline(), timeout=5)
        result = json.loads(list_line)["result"]
        assert {tool["name"] for tool in result["tools"]} == {_TOOL}
    finally:
        os.close(client_to_server_w)  # EOF → the serve loop returns
        await asyncio.wait_for(serve_task, timeout=5)
        read_file.close()


@pytest.mark.asyncio
async def test_immediate_eof_ends_the_serve_loop() -> None:
    """An immediately-closed stdin ends ``serve_stdio`` cleanly (no hang at EOF)."""
    client_to_server_r, client_to_server_w = os.pipe()
    server_to_client_r, server_to_client_w = os.pipe()

    server_stdin = anyio.wrap_file(os.fdopen(client_to_server_r, "r", encoding="utf-8"))
    server_stdout = anyio.wrap_file(os.fdopen(server_to_client_w, "w", encoding="utf-8"))

    os.close(client_to_server_w)  # EOF before any message
    try:
        await asyncio.wait_for(
            serve_stdio(_door_server(), stdin=server_stdin, stdout=server_stdout),
            timeout=5,
        )
    finally:
        os.close(server_to_client_r)
