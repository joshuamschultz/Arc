"""Per-agent MCP server module runtime context.

The decorator-form module (``capabilities.py``) cannot carry state in a closure —
``@capability`` classes are instantiated by the loader with no arguments — so
runtime state (config, the agent's tool registry, the built server) lives on a
:class:`_State` bound to a :class:`contextvars.ContextVar`, configured by the
agent at startup. A plain module global would be silently overwritten by whichever
agent's ``asyncio.Task`` most recently called ``configure()``; an architecture
test fails CI on a module global. This mirrors ``arcagent.modules.pulse._runtime``.
"""

from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from arcagent.modules.mcp_server.config import McpServerConfig

if TYPE_CHECKING:
    from arcagent.core.telemetry import AgentTelemetry
    from arcagent.core.tool_registry import ToolRegistry
    from arcagent.modules.mcp_server.server import McpServer

_logger = logging.getLogger("arcagent.modules.mcp_server._runtime")


@dataclass
class _State:
    """Mutable runtime state shared across the mcp_server capability."""

    config: McpServerConfig
    tool_registry: ToolRegistry | None = None
    telemetry: AgentTelemetry | None = None
    agent_name: str = ""
    server: McpServer | None = None


_state_var: contextvars.ContextVar[_State | None] = contextvars.ContextVar(
    "arcagent_mcp_server_state", default=None
)


def configure(
    *,
    config: dict[str, Any] | McpServerConfig | None = None,
    tool_registry: ToolRegistry | None = None,
    telemetry: AgentTelemetry | None = None,
    agent_name: str = "",
) -> None:
    """Bind module state for the CURRENT asyncio task. Called once at agent startup."""
    if isinstance(config, McpServerConfig):
        cfg = config
    else:
        cfg = McpServerConfig(**(config or {}))
    _state_var.set(
        _State(
            config=cfg,
            tool_registry=tool_registry,
            telemetry=telemetry,
            agent_name=agent_name,
        )
    )


def state() -> _State:
    """Return the configured state. Raises if unconfigured."""
    current = _state_var.get()
    if current is None:
        raise RuntimeError(
            "mcp_server module called before runtime is configured; "
            "agent must call _runtime.configure(...) at startup"
        )
    return current


def bind(state_obj: _State) -> None:
    """Idempotently bind an already-built ``_State`` into the CURRENT task."""
    _state_var.set(state_obj)


def reset() -> None:
    """Test-only: clear runtime state."""
    _state_var.set(None)


__all__ = ["bind", "configure", "reset", "state"]
