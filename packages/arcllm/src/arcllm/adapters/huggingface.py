"""HuggingFace Inference API adapter — OpenAI-compatible cloud endpoint."""

from arcllm.adapters.openai import OpenaiAdapter


class HuggingfaceAdapter(OpenaiAdapter):
    """Thin alias for HuggingFace's OpenAI-compatible Inference API."""

    @property
    def name(self) -> str:
        return "huggingface"
