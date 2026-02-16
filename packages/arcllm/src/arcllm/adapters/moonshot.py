"""Moonshot AI adapter — OpenAI-compatible cloud inference (Kimi models)."""

from arcllm.adapters.openai import OpenaiAdapter


class MoonshotAdapter(OpenaiAdapter):
    """Thin alias for Moonshot AI's OpenAI-compatible API."""

    @property
    def name(self) -> str:
        return "moonshot"
