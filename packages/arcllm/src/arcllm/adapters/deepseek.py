"""DeepSeek adapter — OpenAI-compatible cloud inference."""

from arcllm.adapters.openai import OpenaiAdapter


class DeepseekAdapter(OpenaiAdapter):
    """Thin alias for DeepSeek's OpenAI-compatible API."""

    @property
    def name(self) -> str:
        return "deepseek"
