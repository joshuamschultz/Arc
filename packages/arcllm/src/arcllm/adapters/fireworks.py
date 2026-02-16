"""Fireworks AI adapter — OpenAI-compatible cloud inference."""

from arcllm.adapters.openai import OpenaiAdapter


class FireworksAdapter(OpenaiAdapter):
    """Thin alias for Fireworks AI's OpenAI-compatible API."""

    @property
    def name(self) -> str:
        return "fireworks"
