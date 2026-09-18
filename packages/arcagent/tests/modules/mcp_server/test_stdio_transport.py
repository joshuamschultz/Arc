"""SPEC-082 T-1111 (RED) — the newline-delimited stdio transport.

Phase 5 gives the door a stdio transport — the one a local MCP client (e.g. Claude
Desktop) launches: it reads newline-delimited JSON-RPC messages from a stream,
routes each through ``DoorRouter.handle``, writes each response as one JSON line,
and stops at EOF.

``arcagent.modules.mcp_server.stdio_transport.serve_stdio(router, *, reader, writer)``
is transport-only; it drives whatever router it is handed. These tests hand it an
in-memory ``asyncio.StreamReader`` preloaded with two valid messages (then EOF) and
a fake router, and assert two correct JSON-RPC response lines are written and the
loop returns on EOF. A malformed input line must yield a JSON-RPC error line and the
loop must continue — a single bad line never kills the session (LLM05).

RED: the ``stdio_transport`` module does not exist yet. The import
``from arcagent.modules.mcp_server.stdio_transport import serve_stdio`` fails with
``No module named 'arcagent.modules.mcp_server.stdio_transport'``. It goes GREEN when
T-1111 adds it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from arcagent.modules.mcp_server.stdio_transport import serve_stdio  # RED: module absent today


class _EchoRouter:
    """A stand-in ``DoorRouter`` — records the messages it is handed and echoes them.

    ``serve_stdio`` depends only on ``router.handle(message) -> dict``; the real
    ``DoorRouter`` (T-1110) is exercised by its own contract test. Using a fake here
    keeps this a transport test: it proves framing (one line per response), routing
    (every parsed message reaches ``handle``), and EOF/malformed handling — not the
    router's dispatch logic.
    """

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    async def handle(self, message: dict[str, Any]) -> dict[str, Any]:
        self.seen.append(message)
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": {"echo": message.get("method")}}


class _FakeWriter:
    """A minimal duck-typed asyncio writer capturing everything written."""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.chunks.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self.closed

    def lines(self) -> list[dict[str, Any]]:
        """Every complete written line, parsed as JSON."""
        text = b"".join(self.chunks).decode("utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]


def _reader_with(*raw_lines: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for line in raw_lines:
        reader.feed_data(line)
    reader.feed_eof()
    return reader


@pytest.mark.asyncio
async def test_two_messages_produce_two_response_lines_then_eof() -> None:
    """Two JSON-RPC lines in → two JSON-RPC response lines out; the loop ends on EOF."""
    reader = _reader_with(
        b'{"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}}\n',
        b'{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}\n',
    )
    writer = _FakeWriter()
    router = _EchoRouter()

    await serve_stdio(router, reader=reader, writer=writer)

    responses = writer.lines()
    assert len(responses) == 2
    assert [r["id"] for r in responses] == [1, 2]
    assert responses[0]["result"]["echo"] == "server/discover"
    assert responses[1]["result"]["echo"] == "tools/list"
    # Every parsed message reached the router — framing routed both lines.
    assert [m["method"] for m in router.seen] == ["server/discover", "tools/list"]


@pytest.mark.asyncio
async def test_malformed_line_yields_error_response_and_loop_continues() -> None:
    """A JSON parse error yields a JSON-RPC error line; the next valid line still routes."""
    reader = _reader_with(
        b'{"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}}\n',
        b"{ this is not valid json\n",
        b'{"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}\n',
    )
    writer = _FakeWriter()
    router = _EchoRouter()

    await serve_stdio(router, reader=reader, writer=writer)

    responses = writer.lines()
    assert len(responses) == 3
    # First and third are ok results; the middle is a JSON-RPC error.
    assert "result" in responses[0]
    assert "error" in responses[1]
    assert "result" not in responses[1]
    assert "result" in responses[2] and responses[2]["id"] == 3
    # The malformed line never reached the router; both valid lines did.
    assert [m["method"] for m in router.seen] == ["server/discover", "tools/list"]


@pytest.mark.asyncio
async def test_oversized_line_is_refused_and_loop_continues() -> None:
    """A line over ``max_line_bytes`` is refused pre-parse; the next valid line routes.

    ADVERSARIAL (LLM10): an unbounded request line is a memory-exhaustion
    primitive. It must yield a JSON-RPC error line, never reach the router, and
    never end the session — the following valid line must still be served.
    """
    oversized = b'{"jsonrpc": "2.0", "id": 1, "method": "' + b"A" * 500 + b'"}\n'
    reader = _reader_with(
        oversized,
        b'{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}\n',
    )
    writer = _FakeWriter()
    router = _EchoRouter()

    await serve_stdio(router, reader=reader, writer=writer, max_line_bytes=128)

    responses = writer.lines()
    assert len(responses) == 2
    # The oversized line is a JSON-RPC error, never parsed or routed.
    assert "error" in responses[0]
    assert "result" not in responses[0]
    # The following valid line still routes — the loop kept serving.
    assert "result" in responses[1] and responses[1]["id"] == 2
    assert [m["method"] for m in router.seen] == ["tools/list"]
