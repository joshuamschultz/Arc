"""Google Gemini adapter — OpenAI-compatible endpoint with custom URL path."""

from arcllm.adapters.openai import OpenaiAdapter


class GoogleAdapter(OpenaiAdapter):
    """Translates ArcLLM types to/from the Google Gemini OpenAI-compatible API.

    Google's endpoint uses ``/chat/completions`` directly under the base URL
    instead of the OpenAI convention of ``/v1/chat/completions``.
    """

    @property
    def name(self) -> str:
        return "google"

    def _completions_url(self) -> str:
        # Google's path: {base}/chat/completions (no /v1/ prefix).
        base = self._config.provider.base_url.rstrip("/")
        return f"{base}/chat/completions"
