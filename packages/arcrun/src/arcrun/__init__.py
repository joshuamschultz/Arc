"""arcrun — async execution engine for autonomous agents."""

__version__ = "0.9.0"

from arcrun.builtins import (
    SandboxError,
    SandboxOOMError,
    SandboxRuntimeError,
    SandboxTimeoutError,
    SandboxUnavailableError,
    make_execute_tool,
    run_shell,
)
from arcrun.capabilities import (
    CapabilityProvider,
    CapabilityResult,
    CapabilitySpec,
    StaticProvider,
    detached_context,
    provider_tools,
)
from arcrun.checkpoint import LoopCheckpoint, apply_checkpoint, to_checkpoint
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
    "LoopCheckpoint",
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
    "apply_checkpoint",
    "collect",
    "detached_context",
    "get_strategy_prompts",
    "make_execute_tool",
    "provider_tools",
    "run",
    "run_async",
    "run_shell",
    "run_stream",
    "stream_llm_response",
    "to_checkpoint",
    "verify_chain",
]
