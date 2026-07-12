"""ArcAgent: Enterprise-grade autonomous agent nucleus."""

from importlib.metadata import PackageNotFoundError, version

from arcagent.core.errors import (
    ArcAgentError,
    ConfigError,
    ContextError,
    IdentityError,
    ModuleBusError,
    ToolError,
    ToolVetoedError,
)

__all__ = [
    "ArcAgentError",
    "ConfigError",
    "ContextError",
    "IdentityError",
    "ModuleBusError",
    "ToolError",
    "ToolVetoedError",
]

try:
    __version__ = version("arc-agent")
except PackageNotFoundError:  # reason: source checkout without an installed distribution
    __version__ = "0.16.0"
