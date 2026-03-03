"""OpenAI Chat Completions API adapter."""

import json
from typing import Any

from arcllm.adapters.base import BaseAdapter
from arcllm.config import ProviderConfig
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

# OpenAI finish_reason -> ArcLLM StopReason
_STOP_REASON_MAP: dict[str, StopReason] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "content_filter",
}


class OpenaiAdapter(BaseAdapter):
    """Translates ArcLLM types to/from the OpenAI Chat Completions API."""

    def __init__(
        self,
        config: ProviderConfig,
        model_name: str,
        resolved_api_key: str | None = None,
    ) -> None:
        super().__init__(config, model_name, resolved_api_key=resolved_api_key)
        # Cache headers — api_key is immutable after init.
        self._headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            self._headers["Authorization"] = f"Bearer {self._api_key}"

    @property
    def name(self) -> str:
        return "openai"

    # -- Request building -----------------------------------------------------

    def _build_headers(self) -> dict[str, str]:
        return self._headers

    def _format_tool(self, tool: Tool) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }

    def _format_message(self, message: Message) -> dict[str, Any]:
        """Format a single message. Does NOT handle tool result flattening."""
        if isinstance(message.content, str):
            return {"role": message.role, "content": message.content}

        # Check for ToolUseBlocks (assistant message with tool calls)
        tool_use_blocks = [b for b in message.content if isinstance(b, ToolUseBlock)]
        if tool_use_blocks:
            text_blocks = [b for b in message.content if isinstance(b, TextBlock)]
            text_content = " ".join(b.text for b in text_blocks) if text_blocks else None
            formatted_tool_calls = [
                {
                    "id": b.id,
                    "type": "function",
                    "function": {
                        "name": b.name,
                        "arguments": json.dumps(b.arguments),
                    },
                }
                for b in tool_use_blocks
            ]
            return {
                "role": "assistant",
                "content": text_content,
                "tool_calls": formatted_tool_calls,
            }

        # Plain content blocks (text and images)
        parts: list[dict[str, Any]] = []
        for b in message.content:
            if isinstance(b, TextBlock):
                parts.append({"type": "text", "text": b.text})
            elif isinstance(b, ImageBlock):
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{b.media_type};base64,{b.source}",
                        },
                    }
                )
        if parts:
            return {"role": message.role, "content": parts}
        return {"role": message.role, "content": ""}

    def _format_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Format all messages with tool result flattening.

        A single ArcLLM message with role="tool" and multiple ToolResultBlocks
        expands into multiple OpenAI messages (one per tool result).

        For reasoning models (o-series), ``system`` role is mapped to
        ``developer`` — Azure OpenAI and OpenAI's o-series APIs reject
        ``system`` messages and require ``developer`` instead.
        """
        result: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role == "tool" and isinstance(msg.content, list):
                # Flatten: one message per ToolResultBlock
                for block in msg.content:
                    if isinstance(block, ToolResultBlock):
                        if isinstance(block.content, str):
                            content = block.content
                        else:
                            text_parts = [
                                b.text for b in block.content if isinstance(b, TextBlock)
                            ]
                            content = " ".join(text_parts) if text_parts else ""
                        result.append(
                            {
                                "role": "tool",
                                "tool_call_id": block.tool_use_id,
                                "content": content,
                            }
                        )
                    # Skip non-ToolResultBlock content in tool messages
            else:
                formatted = self._format_message(msg)
                # o-series reasoning models require "developer" instead of "system"
                if self._is_reasoning_model and formatted.get("role") == "system":
                    formatted["role"] = "developer"
                result.append(formatted)
        return result

    @property
    def _is_reasoning_model(self) -> bool:
        """Check if the current model is a reasoning model (o-series)."""
        return bool(self._model_meta and self._model_meta.supports_thinking)

    def _build_request_body(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        formatted = self._format_messages(messages)

        max_tokens, temperature = self._resolve_defaults(**kwargs)

        body: dict[str, Any] = {
            "model": self._model_name,
            "messages": formatted,
        }

        if self._is_reasoning_model:
            # Reasoning models (o3, o4-mini, etc.) reject temperature and
            # max_tokens. Use max_completion_tokens and reasoning_effort.
            body["max_completion_tokens"] = max_tokens
            reasoning_effort = kwargs.get("reasoning_effort", "medium")
            body["reasoning_effort"] = reasoning_effort
        else:
            body["max_tokens"] = max_tokens
            body["temperature"] = temperature

        if tools:
            body["tools"] = [self._format_tool(t) for t in tools]
        return body

    # -- Response parsing -----------------------------------------------------

    def _map_stop_reason(self, finish_reason: str) -> StopReason:
        return _STOP_REASON_MAP.get(finish_reason, "end_turn")

    def _parse_tool_call(self, tc: dict[str, Any]) -> ToolCall:
        func = tc["function"]
        arguments = self._parse_arguments(func["arguments"])
        return ToolCall(id=tc["id"], name=func["name"], arguments=arguments)

    def _parse_usage(self, usage_data: dict[str, Any]) -> Usage:
        prompt_tokens = usage_data["prompt_tokens"]
        completion_tokens = usage_data["completion_tokens"]
        reasoning_tokens = None
        details = usage_data.get("completion_tokens_details")
        if details:
            reasoning_tokens = details.get("reasoning_tokens")
        return Usage(
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
            total_tokens=usage_data.get("total_tokens", prompt_tokens + completion_tokens),
            reasoning_tokens=reasoning_tokens,
        )

    def _parse_response(self, data: dict[str, Any]) -> LLMResponse:
        choice = data["choices"][0]
        message = choice["message"]

        content = message.get("content")
        tool_calls = [self._parse_tool_call(tc) for tc in message.get("tool_calls", [])]
        stop_reason = self._map_stop_reason(choice["finish_reason"])

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage=self._parse_usage(data["usage"]),
            model=data["model"],
            stop_reason=stop_reason,
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
        url = f"{self._config.provider.base_url}/v1/chat/completions"

        response = await self._client.post(url, headers=headers, json=body)

        if response.status_code != 200:
            raise ArcLLMAPIError(
                status_code=response.status_code,
                body=response.text,
                provider=self.name,
                retry_after=self._parse_retry_after(response),
            )

        return self._parse_response(response.json())
