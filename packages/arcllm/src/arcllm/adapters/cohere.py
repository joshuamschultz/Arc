"""Cohere adapter — OpenAI-compatible endpoint."""

from arcllm.adapters.openai import OpenaiAdapter


class CohereAdapter(OpenaiAdapter):
    """Thin alias for Cohere's OpenAI-compatible API."""

    @property
    def name(self) -> str:
        return "cohere"
