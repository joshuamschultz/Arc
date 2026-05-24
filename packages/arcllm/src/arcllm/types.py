"""Core ArcLLM types — the contract everything builds on."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal, TypedDict

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
    TextBlock | ImageBlock | ToolUseBlock | ToolResultBlock,
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

StopReason = Literal["end_turn", "tool_use", "max_tokens", "stop_sequence", "content_filter"]


# ---------------------------------------------------------------------------
# LLM response (normalized across providers)
# ---------------------------------------------------------------------------


class ToolCallDelta(BaseModel):
    """Incremental tool-call fragment from a streaming response.

    ``index`` identifies which tool call this delta belongs to (some
    providers can stream multiple tool calls in parallel). The other
    fields are partial — accumulators are responsible for stitching
    them. ``arguments`` is a string fragment (JSON being assembled char
    by char), not a parsed dict.
    """

    index: int = 0
    id: str | None = None
    name: str | None = None
    arguments: str | None = None


class Delta(BaseModel):
    """One frame from a streaming LLM response.

    Streaming providers emit a sequence of Deltas. Most carry ``text``;
    the final one carries ``stop_reason`` and may carry ``usage``.
    Adapters that don't support real streaming yield a single Delta
    containing the whole response (the default fallback on
    ``LLMProvider.invoke_stream``).
    """

    text: str | None = None
    tool_call: ToolCallDelta | None = None
    usage: Usage | None = None
    stop_reason: StopReason | None = None


class ResponseFormat(TypedDict, total=False):
    """Structured-output enforcement hint, OpenAI-compatible shape.

    Adapters that support a provider-side JSON mode (openai-wire family)
    forward this into the request. Adapters that don't (anthropic, where
    the recommended path is tool_use) raise ``ArcLLMConfigError`` rather
    than silently dropping the kwarg.

    ``type``:
        - ``"text"``: default plain text (no enforcement). Equivalent to
          omitting ``response_format`` entirely.
        - ``"json_object"``: model output must be a valid JSON object.
        - ``"json_schema"``: validates against ``json_schema``. Required
          shape: ``{"name": str, "schema": {...JSON Schema...}, "strict": bool?}``.

    ``json_schema``: required when ``type == "json_schema"``; ignored otherwise.
    """

    type: Literal["text", "json_object", "json_schema"]
    json_schema: dict[str, Any]


class LLMResponse(BaseModel):
    content: str | None = None
    tool_calls: list[ToolCall] = []
    usage: Usage
    model: str
    stop_reason: StopReason
    thinking: str | None = None
    raw: Any = Field(default=None, repr=False, exclude=True)
    metadata: dict[str, Any] | None = None
    cost_usd: float | None = None
    # Populated when the caller passed ``response_format={"type": "json_schema", ...}``
    # and the response parsed as a JSON object matching the schema. Pure
    # convenience — callers can still json.loads(content) themselves.
    parsed_content: dict[str, Any] | None = None


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
        *,
        response_format: ResponseFormat | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Make a single LLM call.

        ``response_format`` (optional): structured-output hint forwarded
        to providers that support a server-side JSON mode (openai-wire
        family). Providers without server-side JSON enforcement
        (anthropic, etc.) raise ``ArcLLMConfigError`` — for those, use
        tool_use with a ``signals_completion`` tool instead.
        """
        ...

    async def invoke_stream(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        *,
        response_format: ResponseFormat | None = None,
        **kwargs: Any,
    ) -> "AsyncIterator[Delta]":
        """Stream incremental Deltas from the model.

        Default implementation calls ``invoke()`` once and yields a
        single Delta carrying the whole response — adapters that
        support a real streaming wire format override this to yield
        per-token Deltas as they arrive. Consumers should treat both
        cases identically; the only observable difference is latency
        between the first token and the last.
        """
        response = await self.invoke(
            messages, tools, response_format=response_format, **kwargs
        )
        yield Delta(
            text=response.content,
            usage=response.usage,
            stop_reason=response.stop_reason,
        )

    @abstractmethod
    def validate_config(self) -> bool: ...

    async def close(self) -> None:  # noqa: B027 — intentional concrete no-op default
        """Release resources held by this provider. No-op by default."""
