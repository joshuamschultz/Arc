"""arcrun — async execution engine for autonomous agents."""

from arcrun.builtins import (
    SandboxError,
    SandboxOOMError,
    SandboxRuntimeError,
    SandboxTimeoutError,
    SandboxUnavailableError,
    make_execute_tool,
    make_spawn_tool,
)
from arcrun.events import GENESIS_PREV_HASH, ChainVerificationResult, Event, EventBus, verify_chain
from arcrun.loop import RunHandle, run, run_async
from arcrun.registry import ToolRegistry
from arcrun.strategies import Strategy
from arcrun.types import LoopResult, SandboxConfig, Tool, ToolContext

__all__ = [
    "run",
    "run_async",
    "RunHandle",
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "LoopResult",
    "SandboxConfig",
    "Event",
    "EventBus",
    "Strategy",
    "make_execute_tool",
    "make_spawn_tool",
    "GENESIS_PREV_HASH",
    "ChainVerificationResult",
    "verify_chain",
    "SandboxError",
    "SandboxUnavailableError",
    "SandboxTimeoutError",
    "SandboxOOMError",
    "SandboxRuntimeError",
]
