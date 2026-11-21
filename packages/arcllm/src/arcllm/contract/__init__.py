"""Vendor-free contract for model providers.

This subpackage defines the minimal interface every model backend must implement:
message and content types, tool definitions, usage tracking, streaming protocol,
and the ModelProvider abstract base class. NO HTTP, NO vendor code, NO imports
of other Arc packages.

Backends (direct-HTTP adapters, TrueFoundry AI Gateway, LiteLLM, etc.) conform
to this contract. Arc's trusted middleware wraps any conforming backend.
"""

from arcllm.contract.errors import ModelStreamProtocolError
from arcllm.contract.middleware import (
    ARC_PRIVATE_KWARGS,
    Middleware,
    strip_arc_private_kwargs,
)
from arcllm.contract.types import (
    ContentBlock,
    Delta,
    ImageBlock,
    LLMResponse,
    Message,
    ModelProvider,
    ResponseFormat,
    StopReason,
    StreamAccumulator,
    TextBlock,
    Tool,
    ToolCall,
    ToolCallDelta,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)

__all__ = [
    # Errors
    "ModelStreamProtocolError",
    # Middleware
    "ARC_PRIVATE_KWARGS",
    "Middleware",
    "strip_arc_private_kwargs",
    # Types
    "ContentBlock",
    "Delta",
    "ImageBlock",
    "LLMResponse",
    "Message",
    "ModelProvider",
    "ResponseFormat",
    "StopReason",
    "StreamAccumulator",
    "TextBlock",
    "Tool",
    "ToolCall",
    "ToolCallDelta",
    "ToolResultBlock",
    "ToolUseBlock",
    "Usage",
]
