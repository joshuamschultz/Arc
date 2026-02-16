"""Mistral AI adapter — OpenAI-compatible with quirk overrides."""

from typing import Any

from arcllm.adapters.openai import OpenaiAdapter
from arcllm.types import Message, StopReason, Tool

# Mistral finish_reason -> ArcLLM StopReason.
# "model_length" is Mistral-specific (total context exhausted vs output limit).
_MISTRAL_STOP_REASON_MAP: dict[str, StopReason] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "model_length": "max_tokens",
    "content_filter": "content_filter",
}


class MistralAdapter(OpenaiAdapter):
    """Translates ArcLLM types to/from the Mistral API.

    Mistral is OpenAI-compatible with these quirks:
    - tool_choice "required" maps to "any"
    - "model_length" is an additional stop reason (maps to max_tokens)
    """

    @property
    def name(self) -> str:
        return "mistral"

    def _build_request_body(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        body = super()._build_request_body(messages, tools, **kwargs)

        # Translate tool_choice: "required" -> "any" (Mistral-specific)
        tool_choice = kwargs.get("tool_choice")
        if tool_choice == "required":
            body["tool_choice"] = "any"
        elif tool_choice is not None:
            body["tool_choice"] = tool_choice

        return body

    def _map_stop_reason(self, finish_reason: str) -> StopReason:
        return _MISTRAL_STOP_REASON_MAP.get(finish_reason, "end_turn")
