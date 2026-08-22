"""Anthropic Messages API adapter."""

import json
from collections.abc import AsyncIterator
from typing import Any

from arcllm.adapters.base import BaseAdapter
from arcllm.exceptions import ArcLLMAPIError, ArcLLMConfigError, ArcLLMStreamProtocolError
from arcllm.types import (
    Delta,
    ImageBlock,
    LLMResponse,
    Message,
    ResponseFormat,
    StopReason,
    TextBlock,
    Tool,
    ToolCall,
    ToolCallDelta,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)

ANTHROPIC_API_VERSION = "2023-06-01"

# Anthropic caps a request at 4 cache breakpoints. The last tool and the
# conversation tail take one each; the rest is the system's budget.
_MAX_SYSTEM_SEGMENTS = 2

# Anthropic has no server-side JSON mode. Its native structured-output path is a
# forced tool call: the schema goes in as the tool's input_schema and the model
# answers by filling it. Translating response_format into that shape is this
# adapter's job — the caller passes the same kwarg to every provider.
_STRUCTURED_TOOL_NAME = "structured_output"
_STRUCTURED_TOOL_DESCRIPTION = (
    "Return your answer as a structured object, in the shape the system prompt "
    "asks for. Call this tool exactly once and emit nothing else."
)

# Anthropic stop_reason -> ArcLLM StopReason
_ANTHROPIC_STOP_REASON_MAP: dict[str, StopReason] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
}


class AnthropicAdapter(BaseAdapter):
    """Translates ArcLLM types to/from the Anthropic Messages API."""

    @property
    def name(self) -> str:
        return "anthropic"

    # -- Request building -----------------------------------------------------

    def _build_headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        }

    def _extract_system(self, messages: list[Message]) -> tuple[list[str], list[Message]]:
        """Separate system messages from the rest.

        Anthropic takes `system` as a top-level param, not in messages. Each
        system message is one *cache segment*: the caller orders them
        most-stable first and every segment end gets its own breakpoint, so a
        change confined to a later segment still reads the earlier segments'
        cache. Empty segments are dropped — an empty text block is invalid on
        the wire, and a blank segment would waste a breakpoint.
        """
        system_parts: list[str] = []
        remaining: list[Message] = []
        for msg in messages:
            if msg.role == "system":
                if isinstance(msg.content, str) and msg.content:
                    system_parts.append(msg.content)
            else:
                remaining.append(msg)
        return system_parts, remaining

    def _format_content_block(
        self, block: TextBlock | ImageBlock | ToolUseBlock | ToolResultBlock
    ) -> dict[str, Any]:
        if isinstance(block, TextBlock):
            return {"type": "text", "text": block.text}
        if isinstance(block, ImageBlock):
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": block.media_type,
                    "data": block.source,
                },
            }
        if isinstance(block, ToolUseBlock):
            return {
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.arguments,
            }
        if isinstance(block, ToolResultBlock):
            if isinstance(block.content, str):
                content: Any = block.content
            else:
                content = [self._format_content_block(b) for b in block.content]
            return {
                "type": "tool_result",
                "tool_use_id": block.tool_use_id,
                "content": content,
            }
        raise ValueError(f"Unknown content block type: {type(block)}")

    def _format_message(self, message: Message) -> dict[str, Any]:
        role = "user" if message.role == "tool" else message.role
        if isinstance(message.content, str):
            content: Any = message.content
        else:
            content = [self._format_content_block(b) for b in message.content]
        return {"role": role, "content": content}

    def _format_tool(self, tool: Tool) -> dict[str, Any]:
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.parameters,
        }

    # -- Prompt caching -------------------------------------------------------
    #
    # Anthropic caches the longest stable prefix ending at a `cache_control`
    # breakpoint, ordered tools -> system -> messages. A later breakpoint also
    # reads caches written by earlier ones (cascade), so <=3 fixed breakpoints
    # (last tool, system, last message block) cover the whole prefix regardless
    # of conversation length. Placement lives entirely here: `cache_control` is
    # an Anthropic wire specific and must not leak into shared arcllm types,
    # arcrun, or arcagent (SPEC-029 D-393).

    def _cache_control(self) -> dict[str, str]:
        """The ephemeral cache_control marker, honoring the configured TTL."""
        marker: dict[str, str] = {"type": "ephemeral"}
        if self._config.provider.cache_ttl == "1h":
            marker["ttl"] = "1h"
        return marker

    def _system_blocks(self, parts: list[str]) -> list[dict[str, Any]]:
        """One cached text block per system segment, in caller order.

        Anthropic allows 4 breakpoints per request; the last tool and the
        conversation tail claim two, leaving ``_MAX_SYSTEM_SEGMENTS`` for the
        system. Over that budget the overflow is folded into the last block
        rather than raising: caching is an optimization, and `system_prompt` is
        public arcrun API, so a standalone caller passing three segments must
        not have every request fail over a cache hint. The earlier segments keep
        their own breakpoints because they are the more stable — and therefore
        more valuable — prefixes; only the tail loses its separate entry.
        """
        if len(parts) > _MAX_SYSTEM_SEGMENTS:
            keep = parts[: _MAX_SYSTEM_SEGMENTS - 1]
            parts = [*keep, "\n".join(parts[_MAX_SYSTEM_SEGMENTS - 1 :])]
        return [
            {"type": "text", "text": text, "cache_control": self._cache_control()}
            for text in parts
        ]

    def _apply_last_message_breakpoint(self, formatted: list[dict[str, Any]]) -> None:
        """Mark the tail of the conversation as the rolling cache breakpoint.

        String content is promoted to a one-block list so the marker can
        attach; a block list gets the marker on its last block.
        """
        content = formatted[-1]["content"]
        if isinstance(content, str):
            formatted[-1]["content"] = [
                {"type": "text", "text": content, "cache_control": self._cache_control()}
            ]
        elif content:
            content[-1] = {**content[-1], "cache_control": self._cache_control()}

    def _build_request_body(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        system_parts, remaining = self._extract_system(messages)
        formatted = [self._format_message(m) for m in remaining]
        caching = self._config.provider.enable_prompt_caching

        max_tokens, temperature = self._resolve_defaults(**kwargs)

        body: dict[str, Any] = {
            "model": self._model_name,
            "max_tokens": max_tokens,
            "messages": formatted,
        }
        # Models declaring supports_temperature = false (Claude 5 family)
        # reject non-default sampling params with HTTP 400 — omit the knob
        # entirely, even when a caller passes it (eval configs set it
        # generically across models).
        if self._model_meta is None or self._model_meta.supports_temperature:
            body["temperature"] = temperature
        if system_parts:
            # A cache breakpoint can only attach to a content-block list, so
            # promote the system segments to blocks when caching is on; collapse
            # to the plain string form otherwise.
            body["system"] = (
                self._system_blocks(system_parts) if caching else "\n".join(system_parts)
            )
        if tools:
            formatted_tools = [self._format_tool(t) for t in tools]
            if caching:
                formatted_tools[-1] = {
                    **formatted_tools[-1],
                    "cache_control": self._cache_control(),
                }
            body["tools"] = formatted_tools
            tool_choice = kwargs.get("tool_choice")
            if tool_choice is not None:
                body["tool_choice"] = tool_choice
        if caching and formatted:
            self._apply_last_message_breakpoint(formatted)
        rf = self._validate_response_format(kwargs.get("response_format"))
        if rf is not None:
            self._apply_structured_output(body, rf, tools)
        return body

    def _apply_structured_output(
        self, body: dict[str, Any], rf: dict[str, Any], tools: list[Tool] | None
    ) -> None:
        """Carry ``response_format`` as the forced tool Anthropic answers with."""
        if tools:
            raise ArcLLMConfigError(
                "response_format cannot be combined with tools on Anthropic: "
                "structured output is itself a forced tool call, which would "
                "disable the tools you passed."
            )
        if self._model_meta is not None and not self._model_meta.supports_tools:
            raise ArcLLMConfigError(
                f"Model {self._model_name!r} is not marked tool-capable in provider "
                "metadata; Anthropic structured output needs a forced tool call."
            )
        tool = self._structured_tool(rf)
        body["tools"] = [tool]
        body["tool_choice"] = {"type": "tool", "name": tool["name"]}

    def _structured_tool(self, rf: dict[str, Any]) -> dict[str, Any]:
        """Build the synthetic tool whose input_schema is the requested shape.

        ``json_object`` has no schema to carry, so the open object schema lets
        the model return whatever shape the prompt asked for.
        """
        schema = rf.get("json_schema") or {}
        return {
            "name": str(schema.get("name") or _STRUCTURED_TOOL_NAME),
            "description": str(schema.get("description") or _STRUCTURED_TOOL_DESCRIPTION),
            "input_schema": schema.get("schema") or {"type": "object"},
        }

    def _structured_tool_name(self, **kwargs: Any) -> str | None:
        """Name of this call's forced structured-output tool, if it has one."""
        rf = self._validate_response_format(kwargs.get("response_format"))
        return self._structured_tool(rf)["name"] if rf is not None else None

    # -- Response parsing -----------------------------------------------------

    def _map_stop_reason(self, raw_reason: str) -> StopReason:
        return _ANTHROPIC_STOP_REASON_MAP.get(raw_reason, "end_turn")

    def _parse_tool_call(self, block: dict[str, Any]) -> ToolCall:
        arguments = self._parse_arguments(block["input"])
        return ToolCall(id=block["id"], name=block["name"], arguments=arguments)

    def _parse_usage(self, usage_data: dict[str, Any]) -> Usage:
        input_tokens = usage_data["input_tokens"]
        output_tokens = usage_data["output_tokens"]
        return Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cache_read_tokens=usage_data.get("cache_read_input_tokens"),
            cache_write_tokens=usage_data.get("cache_creation_input_tokens"),
        )

    def _parse_response(
        self, data: dict[str, Any], structured_tool: str | None = None
    ) -> LLMResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        thinking_parts: list[str] = []
        parsed_content: dict[str, Any] | None = None

        for block in data.get("content", []):
            block_type = block.get("type")
            if block_type == "text":
                text_parts.append(block["text"])
            elif block_type == "tool_use":
                if block["name"] == structured_tool and isinstance(block["input"], dict):
                    parsed_content = block["input"]
                else:
                    tool_calls.append(self._parse_tool_call(block))
            elif block_type == "thinking":
                thinking_parts.append(block["thinking"])

        content = "\n".join(text_parts) if text_parts else None
        thinking = "\n".join(thinking_parts) if thinking_parts else None
        stop_reason = self._map_stop_reason(data["stop_reason"])
        if parsed_content is not None:
            # The forced tool is the answer, not work for the agent loop: surface
            # it as content and end the turn, so no caller waits on a tool result
            # for a tool that was never registered.
            content = json.dumps(parsed_content)
            stop_reason = "end_turn"

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage=self._parse_usage(data["usage"]),
            model=data["model"],
            stop_reason=stop_reason,
            thinking=thinking,
            parsed_content=parsed_content,
            raw=data,
        )

    # -- Public API -----------------------------------------------------------

    async def invoke(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        self._check_tool_capability(tools)
        headers = self._build_headers()
        body = self._build_request_body(messages, tools, **kwargs)
        url = f"{self._config.provider.base_url}/v1/messages"

        response = await self._client.post(url, headers=headers, json=body)

        if response.status_code != 200:
            raise ArcLLMAPIError(
                status_code=response.status_code,
                body=response.text,
                provider=self.name,
                retry_after=self._parse_retry_after(response),
            )

        return self._parse_response(response.json(), self._structured_tool_name(**kwargs))

    async def invoke_stream(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        *,
        response_format: ResponseFormat | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[Delta]:
        """Stream public Messages API deltas without exposing private blocks."""
        self._check_tool_capability(tools)
        body = self._build_request_body(messages, tools, response_format=response_format, **kwargs)
        body["stream"] = True
        url = f"{self._config.provider.base_url}/v1/messages"
        input_tokens = 0
        saw_stop = False

        async with self._client.stream(
            "POST", url, headers=self._build_headers(), json=body
        ) as response:
            if response.status_code != 200:
                error_body = await response.aread()
                raise ArcLLMAPIError(
                    status_code=response.status_code,
                    body=error_body.decode("utf-8", errors="replace"),
                    provider=self.name,
                    retry_after=self._parse_retry_after(response),
                )
            event_name: str | None = None
            async for line in response.aiter_lines():
                if not line:
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                if event_name is None:
                    raise ArcLLMStreamProtocolError("event data has no event type")
                try:
                    payload = json.loads(line[5:].strip())
                except json.JSONDecodeError as error:
                    raise ArcLLMStreamProtocolError("malformed event data") from error
                if not isinstance(payload, dict):
                    raise ArcLLMStreamProtocolError("event data is not an object")
                if event_name == "message_start":
                    usage = payload.get("message", {}).get("usage", {})
                    if isinstance(usage, dict):
                        input_tokens = usage.get("input_tokens", 0)
                elif event_name == "message_stop":
                    saw_stop = True
                else:
                    delta = self._parse_stream_event(event_name, payload, input_tokens)
                    if delta is not None:
                        yield delta
                event_name = None
        if not saw_stop:
            raise ArcLLMStreamProtocolError("stream ended before message_stop")

    def _parse_stream_event(
        self, event_name: str, payload: dict[str, Any], input_tokens: int
    ) -> Delta | None:
        """Normalize one public Anthropic SSE event or safely ignore it."""
        if event_name in {"ping", "message_start"}:
            return None
        if event_name == "error":
            raise ArcLLMStreamProtocolError("provider reported a stream error")
        if event_name == "content_block_start":
            block = payload.get("content_block")
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                return None
            return Delta(
                tool_call=ToolCallDelta(
                    index=self._stream_index(payload),
                    id=self._stream_string(block, "id"),
                    name=self._stream_string(block, "name"),
                )
            )
        if event_name == "content_block_delta":
            delta = payload.get("delta")
            if not isinstance(delta, dict):
                raise ArcLLMStreamProtocolError("content delta is not an object")
            if delta.get("type") == "text_delta":
                text = delta.get("text")
                if not isinstance(text, str):
                    raise ArcLLMStreamProtocolError("text delta is not a string")
                return Delta(text=text)
            if delta.get("type") == "input_json_delta":
                arguments = delta.get("partial_json")
                if not isinstance(arguments, str):
                    raise ArcLLMStreamProtocolError("tool arguments are not a string")
                return Delta(
                    tool_call=ToolCallDelta(index=self._stream_index(payload), arguments=arguments)
                )
            return None
        if event_name == "message_delta":
            delta_data = payload.get("delta")
            usage_data = payload.get("usage")
            if not isinstance(delta_data, dict) or not isinstance(usage_data, dict):
                raise ArcLLMStreamProtocolError("message delta is malformed")
            raw_reason = delta_data.get("stop_reason")
            stop_reason = (
                self._map_stop_reason(raw_reason) if isinstance(raw_reason, str) else None
            )
            output_tokens = usage_data.get("output_tokens", 0)
            if not isinstance(output_tokens, int):
                raise ArcLLMStreamProtocolError("usage counters are malformed")
            return Delta(
                usage=Usage(
                    input_tokens=usage_data.get("input_tokens", input_tokens),
                    output_tokens=output_tokens,
                    total_tokens=usage_data.get("input_tokens", input_tokens) + output_tokens,
                ),
                stop_reason=stop_reason,
            )
        return None

    @staticmethod
    def _stream_index(payload: dict[str, Any]) -> int:
        index = payload.get("index")
        if not isinstance(index, int) or index < 0:
            raise ArcLLMStreamProtocolError("tool index is invalid")
        return index

    @staticmethod
    def _stream_string(payload: dict[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            raise ArcLLMStreamProtocolError("tool metadata is invalid")
        return value
