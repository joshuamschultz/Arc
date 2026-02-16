"""Core ArcLLM types — the contract everything builds on."""

from abc import ABC, abstractmethod
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# ContentBlock variants (discriminated on `type` field)
# ---------------------------------------------------------------------------


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ImageBlock(BaseModel):
    type: Literal["image"] = "image"
    source: str
    media_type: str


class ToolUseBlock(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    arguments: dict[str, Any]


class ToolResultBlock(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: "str | list[ContentBlock]"


# Discriminated union — pydantic checks `type` field to pick the right model.
ContentBlock = Annotated[
    Union[TextBlock, ImageBlock, ToolUseBlock, ToolResultBlock],
    Field(discriminator="type"),
]

# Resolve the forward reference in ToolResultBlock.content.
ToolResultBlock.model_rebuild()


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[ContentBlock]


# ---------------------------------------------------------------------------
# Tool definition (sent to LLM)
# ---------------------------------------------------------------------------


class Tool(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


# ---------------------------------------------------------------------------
# Tool call (returned by LLM)
# ---------------------------------------------------------------------------


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]


# ---------------------------------------------------------------------------
# Usage tracking
# ---------------------------------------------------------------------------


class Usage(BaseModel):
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None


# ---------------------------------------------------------------------------
# Stop reason (normalized across providers)
# ---------------------------------------------------------------------------

StopReason = Literal[
    "end_turn", "tool_use", "max_tokens", "stop_sequence", "content_filter"
]


# ---------------------------------------------------------------------------
# LLM response (normalized across providers)
# ---------------------------------------------------------------------------


class LLMResponse(BaseModel):
    content: str | None = None
    tool_calls: list[ToolCall] = []
    usage: Usage
    model: str
    stop_reason: StopReason
    thinking: str | None = None
    raw: Any = Field(default=None, repr=False, exclude=True)
    metadata: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Provider abstract base class (NOT a pydantic model)
# ---------------------------------------------------------------------------


class LLMProvider(ABC):
    name: str

    @abstractmethod
    async def invoke(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> LLMResponse: ...

    @abstractmethod
    def validate_config(self) -> bool: ...
