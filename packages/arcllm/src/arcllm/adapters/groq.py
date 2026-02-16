"""Groq adapter — OpenAI-compatible fast inference."""

from arcllm.adapters.openai import OpenaiAdapter


class GroqAdapter(OpenaiAdapter):
    """Thin alias for Groq's OpenAI-compatible API."""

    @property
    def name(self) -> str:
        return "groq"
