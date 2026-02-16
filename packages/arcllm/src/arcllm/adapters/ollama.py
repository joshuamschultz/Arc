"""Ollama adapter — OpenAI-compatible local inference server."""

from arcllm.adapters.openai import OpenaiAdapter


class OllamaAdapter(OpenaiAdapter):
    """Thin alias for Ollama's OpenAI-compatible API."""

    @property
    def name(self) -> str:
        return "ollama"
