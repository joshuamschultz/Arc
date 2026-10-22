"""Core ArcLLM types — re-exported from arcllm.contract for backward compatibility.

New code should import from arcllm.contract or use the top-level arcllm exports.
The contract types are vendor-free and define the interface every backend implements.
"""

# Re-export all contract types for backward compatibility
from arcllm.contract import (
    ContentBlock,
    Delta,
    ImageBlock,
    LLMResponse,
    Message,
    ModelProvider,
    ModelStreamProtocolError,
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

# Legacy alias
LLMProvider = ModelProvider
ArcLLMStreamProtocolError = ModelStreamProtocolError

__all__ = [
    "ContentBlock",
    "Delta",
    "ImageBlock",
    "LLMProvider",
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
    "ArcLLMStreamProtocolError",
    "ModelStreamProtocolError",
]
