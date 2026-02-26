"""xAI Grok adapter — OpenAI-compatible cloud inference."""

from arcllm.adapters.openai import OpenaiAdapter


class XaiAdapter(OpenaiAdapter):
    """Thin alias for xAI's OpenAI-compatible API."""

    @property
    def name(self) -> str:
        return "xai"
