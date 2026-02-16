"""HuggingFace TGI adapter — OpenAI-compatible self-hosted inference."""

from arcllm.adapters.openai import OpenaiAdapter


class Huggingface_TgiAdapter(OpenaiAdapter):
    """Thin alias for HuggingFace Text Generation Inference (TGI)."""

    @property
    def name(self) -> str:
        return "huggingface_tgi"
