"""Together AI adapter — OpenAI-compatible cloud inference."""

from arcllm.adapters.openai import OpenaiAdapter


class TogetherAdapter(OpenaiAdapter):
    """Thin alias for Together AI's OpenAI-compatible API."""

    @property
    def name(self) -> str:
        return "together"
