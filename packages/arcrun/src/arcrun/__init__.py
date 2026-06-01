"""arcrun — async execution engine for autonomous agents."""

__version__ = "0.5.0"

from arcrun.builtins import (
    SandboxError,
    SandboxOOMError,
    SandboxRuntimeError,
    SandboxTimeoutError,
    SandboxUnavailableError,
    make_execute_tool,
)
from arcrun.capabilities import (
    CapabilityProvider,
    CapabilityResult,
    CapabilitySpec,
    StaticProvider,
    detached_context,
    provider_tools,
)
from arcrun.events import GENESIS_PREV_HASH, ChainVerificationResult, Event, EventBus, verify_chain
from arcrun.loop import RunHandle, run, run_async
from arcrun.prompts import get_strategy_prompts
from arcrun.registry import ToolRegistry
from arcrun.strategies import Strategy
from arcrun.streams import (
    RunResult,
    StreamEvent,
    TokenEvent,
    ToolEndEvent,
    ToolStartEvent,
    TurnEndEvent,
    collect,
    run_stream,
    stream_llm_response,
)
from arcrun.types import LoopResult, SandboxConfig, Tool, ToolContext

__all__ = [
    "GENESIS_PREV_HASH",
    "CapabilityProvider",
    "CapabilityResult",
    "CapabilitySpec",
    "ChainVerificationResult",
    "Event",
    "EventBus",
    "LoopResult",
    "RunHandle",
    "RunResult",
    "SandboxConfig",
    "SandboxError",
    "SandboxOOMError",
    "SandboxRuntimeError",
    "SandboxTimeoutError",
    "SandboxUnavailableError",
    "StaticProvider",
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
    "collect",
    "detached_context",
    "get_strategy_prompts",
    "make_execute_tool",
    "provider_tools",
    "run",
    "run_async",
    "run_stream",
    "stream_llm_response",
    "verify_chain",
]
