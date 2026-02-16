"""vLLM adapter — OpenAI-compatible high-performance inference server."""

from arcllm.adapters.openai import OpenaiAdapter


class VllmAdapter(OpenaiAdapter):
    """Thin alias for vLLM's OpenAI-compatible API."""

    @property
    def name(self) -> str:
        return "vllm"
