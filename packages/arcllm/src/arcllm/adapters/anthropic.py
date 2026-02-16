"""Anthropic Messages API adapter."""

from typing import Any

from arcllm.adapters.base import BaseAdapter
from arcllm.exceptions import ArcLLMAPIError
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

    def _extract_system(
        self, messages: list[Message]
    ) -> tuple[str | None, list[Message]]:
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

    def _build_request_body(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        system_text, remaining = self._extract_system(messages)
        formatted = [self._format_message(m) for m in remaining]

        max_tokens, temperature = self._resolve_defaults(**kwargs)

        body: dict[str, Any] = {
            "model": self._model_name,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": formatted,
        }
        if system_text is not None:
            body["system"] = system_text
        if tools:
            body["tools"] = [self._format_tool(t) for t in tools]
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
