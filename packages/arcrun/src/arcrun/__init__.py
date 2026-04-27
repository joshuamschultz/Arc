"""arcrun — async execution engine for autonomous agents."""

__version__ = "0.4.0"

from arcrun.builtins import (
    SandboxError,
    SandboxOOMError,
    SandboxRuntimeError,
    SandboxTimeoutError,
    SandboxUnavailableError,
    make_execute_tool,
)
from arcrun.events import GENESIS_PREV_HASH, ChainVerificationResult, Event, EventBus, verify_chain
from arcrun.loop import RunHandle, run, run_async
from arcrun.prompts import get_strategy_prompts
from arcrun.registry import ToolRegistry
from arcrun.strategies import Strategy
from arcrun.streams import (
    StreamEvent,
    TokenEvent,
    ToolEndEvent,
    ToolStartEvent,
    TurnEndEvent,
    run_stream,
)
from arcrun.types import LoopResult, SandboxConfig, Tool, ToolContext

__all__ = [
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
    "StreamEvent",
    "TokenEvent",
    "Tool",
    "ToolContext",
    "ToolEndEvent",
    "ToolRegistry",
    "ToolStartEvent",
    "TurnEndEvent",
    "__version__",
    "get_strategy_prompts",
    "make_execute_tool",
    "run",
    "run_async",
    "run_stream",
    "verify_chain",
]
