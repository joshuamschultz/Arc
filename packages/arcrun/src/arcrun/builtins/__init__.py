"""Builtin tools shipped with arcrun."""

from arcrun.builtins.execute import make_execute_tool
from arcrun.builtins.spawn import make_spawn_tool

# Error types are importable without docker SDK installed.
# The factory (make_contained_execute_tool) requires docker and must be
# imported directly: from arcrun.builtins.contained_execute import make_contained_execute_tool
from arcrun.builtins.contained_execute import (
    SandboxError,
    SandboxOOMError,
    SandboxRuntimeError,
    SandboxTimeoutError,
    SandboxUnavailableError,
)

__all__ = [
    "make_execute_tool",
    "make_spawn_tool",
    "SandboxError",
    "SandboxUnavailableError",
    "SandboxTimeoutError",
    "SandboxOOMError",
    "SandboxRuntimeError",
]
