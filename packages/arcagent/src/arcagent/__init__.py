"""ArcAgent: Enterprise-grade autonomous agent nucleus."""

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

__version__ = "0.4.0"
