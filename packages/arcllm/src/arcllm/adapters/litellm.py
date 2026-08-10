"""LiteLLM proxy adapter — one OpenAI-compatible gateway in front of every provider.

LiteLLM is not a model vendor. It is a proxy that fronts Anthropic, OpenAI,
Ollama, Bedrock and the rest behind a single OpenAI-format endpoint, so a
deployment keeps routing, key custody, metering and spend in one place instead
of in every agent's config. The wire format is OpenAI Chat Completions, so this
is a thin alias in the same shape as the vLLM adapter.

The model name is whatever the proxy is configured to serve: a symbolic alias
an operator defined (``tier-balanced``), or a model the proxy exposes directly
(``claude-sonnet-5``). This adapter does not validate it — the proxy owns that
catalogue and answers with a clear error when a name is not served, which is
better than a stale list here refusing a model that works.
"""

from arcllm.adapters.openai import OpenaiAdapter


class LitellmAdapter(OpenaiAdapter):
    """Thin alias for the LiteLLM proxy's OpenAI-compatible API."""

    @property
    def name(self) -> str:
        return "litellm"
