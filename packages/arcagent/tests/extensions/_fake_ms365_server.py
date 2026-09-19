"""A tiny spec-conformant fake ``ms-365-mcp-server`` over real stdio (SPEC-084 T-1149).

CON-15 requires the Microsoft connector to be proven against a *real* MCP server on
the real wire, not a hand-rolled :class:`mcp.ClientSession` stub (which is what
``test_microsoft365_source.py`` uses, and which never spawns a process). This module
is a genuine ``mcp``-SDK :class:`~mcp.server.fastmcp.FastMCP` server that runs over
**stdio** exactly as the real Node ``ms-365-mcp-server`` does, so a test that spawns
it as a subprocess exercises the actual Legacy ``initialize`` handshake the SDK
negotiates against ``mcp==1.29.0``.

It is deliberately Microsoft-*shaped*, not Microsoft-*faithful*: it serves the single
allowlisted verb ``list-mail-messages`` and returns the Microsoft Graph envelope shape
(``{"value": [...]}``) the source adapters read — enough to prove probe → list → call
and one source sync end to end, and no more. The tool returns a ``dict``; the SDK
carries that as structured content, which is the shape :class:`SdkMcpClient` adapts to
a plain hook value at its boundary.

Run as a subprocess: ``python <path-to-this-file>``.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

_server = FastMCP(name="ms-365-mcp-server")


@_server.tool(name="list-mail-messages", description="List Outlook messages.")
def list_mail_messages(folder: str = "inbox", top: int = 10, skip: int = 0) -> dict[str, Any]:
    """One scripted Graph mail page. The signature is the served ``inputSchema``."""
    return {
        "value": [
            {
                "id": "m1",
                "lastModifiedDateTime": "2026-01-01T00:00:00Z",
                "conversationId": "c1",
                "bodyPreview": "one",
            }
        ]
    }


if __name__ == "__main__":
    _server.run(transport="stdio")
