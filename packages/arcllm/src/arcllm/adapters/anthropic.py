"""Anthropic Messages API adapter."""

from typing import Any

from arcllm.adapters.base import BaseAdapter
from arcllm.exceptions import ArcLLMAPIError, ArcLLMConfigError
from arcllm.types import (
    ImageBlock,
    LLMResponse,
    Message,
    StopReason,
    TextBlock,
    Tool,
    ToolCall,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)

ANTHROPIC_API_VERSION = "2023-06-01"

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

    def _extract_system(self, messages: list[Message]) -> tuple[str | None, list[Message]]:
        """Separate system messages from the rest.

        Anthropic takes `system` as a top-level param, not in messages.
        Multiple system messages are concatenated with newlines.
        """
        system_parts: list[str] = []
        remaining: list[Message] = []
        for msg in messages:
            if msg.role == "system":
                content = msg.content if isinstance(msg.content, str) else ""
                system_parts.append(content)
            else:
                remaining.append(msg)
        system_text = "\n".join(system_parts) if system_parts else None
        return system_text, remaining

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
        system_text, remaining = self._extract_system(messages)
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
        if system_text is not None:
            # A cache breakpoint can only attach to a content-block list, so
            # promote the system string when caching is on; keep the plain
            # string form otherwise.
            body["system"] = (
                [{"type": "text", "text": system_text, "cache_control": self._cache_control()}]
                if caching
                else system_text
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
        # Anthropic has no server-side JSON mode — the recommended path for
        # structured output is tool_use with a signals_completion tool. Fail
        # loudly rather than silently dropping the kwarg.
        if kwargs.get("response_format") is not None:
            self._validate_response_format(kwargs["response_format"])  # may raise on bad shape
            raise ArcLLMConfigError(
                "Anthropic adapter does not support response_format. "
                "Use a tool with signals_completion=True for structured output."
            )
        return body

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

    def _parse_response(self, data: dict[str, Any]) -> LLMResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        thinking_parts: list[str] = []

        for block in data.get("content", []):
            block_type = block.get("type")
            if block_type == "text":
                text_parts.append(block["text"])
            elif block_type == "tool_use":
                tool_calls.append(self._parse_tool_call(block))
            elif block_type == "thinking":
                thinking_parts.append(block["thinking"])

        content = "\n".join(text_parts) if text_parts else None
        thinking = "\n".join(thinking_parts) if thinking_parts else None

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage=self._parse_usage(data["usage"]),
            model=data["model"],
            stop_reason=self._map_stop_reason(data["stop_reason"]),
            thinking=thinking,
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

        return self._parse_response(response.json())
