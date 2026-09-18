"""SPEC-082 T-1111 / COMP-001 — the newline-delimited stdio transport.

The transport a local MCP client (e.g. Claude Desktop) launches: it reads
newline-delimited JSON-RPC messages from a stream, routes each through
:meth:`~arcagent.modules.mcp_server.router.DoorRouter.handle`, writes each
response as one JSON line, and stops at EOF.

It is transport-only — it drives whatever object with an
``async handle(message) -> dict`` it is handed, so the same router serves stdio
and HTTP identically. Two defensive properties hold per line, so one bad line
never kills the session:

- **Malformed JSON** yields a JSON-RPC error line; the loop continues (LLM05).
- **An oversized line** is refused before it is parsed, and never buffered past
  the cap — an unbounded line with no newline is a memory-exhaustion primitive
  (LLM10). The cap is enforced two ways: the reader's own byte limit (which
  raises before it buffers past the cap) and an explicit ``max_line_bytes``
  check on any line that fits the reader but exceeds the policy cap.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from arcagent.modules.mcp_server.router import INVALID_REQUEST, _error

#: Default per-line cap — mirrors the HTTP door's 8 MiB body cap.
_DEFAULT_MAX_LINE_BYTES = 8 * 1024 * 1024

#: JSON-RPC error code for a refused oversized request line.
_LINE_TOO_LARGE = -32600


class _Router(Protocol):
    """The one method ``serve_stdio`` needs — a transport-agnostic handler."""

    async def handle(self, message: dict[str, Any]) -> dict[str, Any]: ...


class _Reader(Protocol):
    """The ``asyncio.StreamReader`` surface ``serve_stdio`` reads from."""

    async def readline(self) -> bytes: ...


class _Writer(Protocol):
    """The ``asyncio.StreamWriter`` surface ``serve_stdio`` writes to."""

    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...


async def serve_stdio(
    router: _Router,
    *,
    reader: _Reader,
    writer: _Writer,
    max_line_bytes: int = _DEFAULT_MAX_LINE_BYTES,
) -> None:
    """Serve newline-delimited JSON-RPC over ``reader``/``writer`` until EOF.

    Each input line is routed through ``router.handle`` and its response written
    as one JSON line. A malformed line yields a JSON-RPC error line; an oversized
    line is refused. Either way the loop keeps serving — a single bad line never
    ends the session.
    """
    while True:
        try:
            raw = await reader.readline()
        except ValueError:
            # readline overran the reader's byte limit before finding a newline:
            # an unbounded line with no separator. The reader has already dropped
            # its buffer, so refusing and continuing is memory-safe (LLM10).
            await _write_line(writer, _error(None, _LINE_TOO_LARGE, "request line too large"))
            continue
        if not raw:
            return  # EOF
        if len(raw) > max_line_bytes:
            await _write_line(writer, _error(None, _LINE_TOO_LARGE, "request line too large"))
            continue
        line = raw.strip()
        if not line:
            continue  # blank keep-alive line
        try:
            message = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            await _write_line(writer, _error(None, INVALID_REQUEST, "malformed JSON"))
            continue
        response = await router.handle(message)
        await _write_line(writer, response)


async def _write_line(writer: _Writer, payload: dict[str, Any]) -> None:
    """Write one JSON-RPC envelope as a single newline-terminated line."""
    writer.write(json.dumps(payload).encode("utf-8") + b"\n")
    await writer.drain()


__all__ = ["serve_stdio"]
