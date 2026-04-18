"""arcrun — async execution engine for autonomous agents."""

__version__ = "0.4.0"

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
from arcrun.prompts import get_strategy_prompts
from arcrun.registry import ToolRegistry
from arcrun.strategies import Strategy
from arcrun.types import LoopResult, SandboxConfig, Tool, ToolContext

__all__ = [
    "__version__",
    "GENESIS_PREV_HASH",
    "ChainVerificationResult",
    "Event",
    "EventBus",
    "LoopResult",
    "RunHandle",
    "SandboxConfig",
    "SandboxError",
    "SandboxOOMError",
    "SandboxRuntimeError",
    "SandboxTimeoutError",
    "SandboxUnavailableError",
    "Strategy",
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "get_strategy_prompts",
    "make_execute_tool",
    "make_spawn_tool",
    "run",
    "run_async",
    "verify_chain",
]
