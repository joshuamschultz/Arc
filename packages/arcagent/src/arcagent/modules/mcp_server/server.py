"""The read-only MCP tool catalog: allowlist-filterable ``tools/list`` descriptors.

It reads the agent's own tool catalog from the
:class:`~arcagent.core.tool_registry.ToolRegistry` and maps it to MCP tool
descriptors. The SDK server (:mod:`arcagent.modules.mcp_server.sdk_server`) reuses
this catalog for its ``list_tools`` handler; the ``initialize`` handshake and the
wire itself are the SDK's, not this module's.

This is listing only. ``tools/call`` dispatch, inbound identity, the exposure
allowlist, and the transports live in their own modules; a door with no allowlist
filter yet lists the full catalog.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from arcagent.core.tool_registry import RegisteredTool, ToolRegistry

#: Fallback page size when the caller supplies none.
DEFAULT_PAGE_SIZE = 100


@dataclass(frozen=True)
class ToolPage:
    """One page of ``tools/list``: the tool descriptors and the next cursor.

    ``next_cursor`` is ``None`` on the final page — the client stops paging when
    the cursor is absent, exactly as this agent's own client does.
    """

    tools: list[dict[str, Any]]
    next_cursor: str | None


class McpServer:
    """Answer the read-only MCP methods from the agent's tool catalog.

    Args:
        registry: The agent's tool registry; its ``tools`` mapping is the catalog.
        server_name: Identity reported to clients, for their logs and ours.
        page_size: Maximum tools returned per ``tools/list`` page.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        server_name: str = "arc",
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        self._registry = registry
        self._server_name = server_name
        self._page_size = max(page_size, 1)

    def list_tools(self, *, cursor: str | None = None) -> ToolPage:
        """One page of the tool catalog, mapped to MCP tool descriptors.

        The catalog is ordered by name so a cursor (a byte offset into that stable
        order) is meaningful across calls.
        """
        names = sorted(self._registry.tools)
        start = _offset(cursor)
        window = names[start : start + self._page_size]
        tools = [self._describe(self._registry.tools[name]) for name in window]
        end = start + len(window)
        next_cursor = str(end) if end < len(names) else None
        return ToolPage(tools=tools, next_cursor=next_cursor)

    @staticmethod
    def _describe(tool: RegisteredTool) -> dict[str, Any]:
        """Map one :class:`RegisteredTool` to an MCP tool descriptor.

        ``inputSchema`` is a JSON Schema object; a tool that declared none gets the
        empty-object schema rather than a missing key, which a strict client would
        reject.
        """
        return {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.input_schema or {"type": "object", "properties": {}},
        }


def _offset(cursor: str | None) -> int:
    """Parse an opaque cursor into a non-negative offset; garbage restarts at 0."""
    if not cursor:
        return 0
    try:
        return max(int(cursor), 0)
    except ValueError:
        return 0


__all__ = ["DEFAULT_PAGE_SIZE", "McpServer", "ToolPage"]
