"""Builtin tools shipped with arcrun."""

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
from arcrun.builtins.execute import make_execute_tool
from arcrun.builtins.task_complete import (
    TaskCompleteArgs,
    TaskStatus,
    make_budget_breach_args,
    make_task_complete_tool,
)

__all__ = [
    "SandboxError",
    "SandboxOOMError",
    "SandboxRuntimeError",
    "SandboxTimeoutError",
    "SandboxUnavailableError",
    "TaskCompleteArgs",
    "TaskStatus",
    "make_budget_breach_args",
    "make_execute_tool",
    "make_task_complete_tool",
]
