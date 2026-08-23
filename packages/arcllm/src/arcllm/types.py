"""Core ArcLLM types — the contract everything builds on."""

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from arcllm.exceptions import ArcLLMStreamProtocolError

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

    model_config = ConfigDict(extra="forbid")

    text: str | None = None
    tool_call: ToolCallDelta | None = None
    usage: Usage | None = None
    stop_reason: StopReason | None = None
    metadata: dict[str, Any] | None = None


class ResponseFormat(TypedDict, total=False):
    """Structured-output enforcement hint, OpenAI-compatible shape.

    Every adapter builds its provider's own structured-output payload from
    this one hint: the openai-wire family forwards it as ``response_format``,
    anthropic carries it as a forced tool call. A caller passes the same
    kwarg everywhere and reads the object back off ``parsed_content``.

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
    tool_calls: list[ToolCall] = Field(default_factory=list)
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


@dataclass
class _ToolCallParts:
    """One provider tool call reconstructed from its wire fragments."""

    id: str | None = None
    name: str | None = None
    arguments: list[str] = field(default_factory=list)


class StreamAccumulator:
    """Materialize normalized :class:`LLMResponse` data from ``Delta`` frames.

    Provider adapters own wire parsing and yield provider-neutral deltas. This
    accumulator owns only normalized data reconstruction, so a loop can consume
    native deltas while still receiving the same complete response shape as a
    blocking invocation. It deliberately has no reasoning channel.
    """

    def __init__(self, *, model: str) -> None:
        self._model = model
        self._text: list[str] = []
        self._tools: dict[int, _ToolCallParts] = {}
        self._usage: Usage | None = None
        self._stop_reason: StopReason | None = None
        self._metadata: dict[str, Any] = {}
        self._error: str | None = None

    def add(self, delta: Delta) -> None:
        """Accept one normalized frame without exposing its raw payload."""
        if delta.text is not None:
            self._text.append(delta.text)
        if delta.tool_call is not None:
            self._add_tool_call(delta.tool_call)
        if delta.usage is not None:
            self._usage = delta.usage
        if delta.stop_reason is not None:
            self._stop_reason = delta.stop_reason
        if delta.metadata:
            self._metadata.update(delta.metadata)

    def build(self) -> LLMResponse:
        """Return a validated response or a sanitized protocol error."""
        if self._error is not None:
            raise ArcLLMStreamProtocolError(self._error)
        tool_calls = [
            self._build_tool_call(index, self._tools[index]) for index in sorted(self._tools)
        ]
        return LLMResponse(
            content="".join(self._text) or None,
            tool_calls=tool_calls,
            usage=self._usage or Usage(input_tokens=0, output_tokens=0, total_tokens=0),
            model=self._model,
            stop_reason=self._stop_reason or "end_turn",
            metadata=self._metadata or None,
        )

    def _add_tool_call(self, delta: ToolCallDelta) -> None:
        parts = self._tools.setdefault(delta.index, _ToolCallParts())
        self._set_field(parts, "id", delta.id, "conflicting tool call id")
        self._set_field(parts, "name", delta.name, "conflicting tool call name")
        if delta.arguments is not None:
            parts.arguments.append(delta.arguments)

    def _set_field(
        self,
        parts: _ToolCallParts,
        field_name: Literal["id", "name"],
        value: str | None,
        conflict: str,
    ) -> None:
        if value is None:
            return
        previous = getattr(parts, field_name)
        if previous is not None and previous != value:
            self._error = conflict
            return
        setattr(parts, field_name, value)

    def _build_tool_call(self, index: int, parts: _ToolCallParts) -> ToolCall:
        if parts.id is None or parts.name is None:
            raise ArcLLMStreamProtocolError("streamed tool call is incomplete")
        # A tool that takes no parameters streams no argument text at all (or a
        # single empty fragment). That is a well-formed zero-argument call, not
        # a decode failure — reading it as one ended whole runs on the first
        # such call.
        raw_arguments = "".join(parts.arguments).strip()
        if not raw_arguments:
            return ToolCall(id=parts.id, name=parts.name, arguments={})
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise ArcLLMStreamProtocolError("streamed tool call arguments are malformed") from exc
        if not isinstance(arguments, dict):
            raise ArcLLMStreamProtocolError("streamed tool call arguments must be an object")
        return ToolCall(id=parts.id, name=parts.name, arguments=arguments)


# ---------------------------------------------------------------------------
# Provider abstract base class (NOT a pydantic model)
# ---------------------------------------------------------------------------


class LLMProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier (e.g. "anthropic", "openai"). Read-only."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Resolved model identifier this provider instance targets. Read-only."""

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
        response = await self.invoke(messages, tools, response_format=response_format, **kwargs)
        yield Delta(
            text=response.content,
            usage=response.usage,
            stop_reason=response.stop_reason,
        )

    @abstractmethod
    def validate_config(self) -> bool: ...

    async def close(self) -> None:  # noqa: B027 — intentional concrete no-op default
        """Release resources held by this provider. No-op by default."""
