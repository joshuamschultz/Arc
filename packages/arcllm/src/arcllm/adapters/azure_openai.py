"""Azure OpenAI Service adapter — Azure AI Foundry / GCC deployment support.

Extends the OpenAI adapter with Azure-specific URL construction and
api-key header authentication. Supports both commercial (.azure.com)
and government (.azure.us) endpoints.

Key differences from standard OpenAI:
- URL: {base}/openai/v1/chat/completions (v1 API, no api-version query param)
- Auth: api-key header (not Authorization: Bearer)
- Model field: deployment name, not canonical model name
- content_filter finish_reason from Azure Content Safety (handled by base)
"""

from arcllm.adapters.openai import OpenaiAdapter


class Azure_OpenaiAdapter(OpenaiAdapter):  # noqa: N801 — matches provider name convention
    """Azure OpenAI Service adapter.

    Overrides URL construction and authentication for Azure's v1 API.
    The content_filter finish_reason is already mapped in the base
    OpenaiAdapter's _STOP_REASON_MAP.
    """

    @property
    def name(self) -> str:
        return "azure_openai"

    def _build_headers(self) -> dict[str, str]:
        """Use api-key header instead of Bearer token.

        Azure requires the lowercase ``api-key`` header (case-sensitive).
        """
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["api-key"] = self._api_key
        return headers

    def _completions_url(self) -> str:
        """Azure v1 API endpoint: ``{base}/openai/v1/chat/completions``.

        The v1 API hard-fails (400) if ``?api-version=`` is appended, so no
        query params. ``rstrip('/')`` prevents a double slash on a
        trailing-slash base URL.
        """
        base = self._config.provider.base_url.rstrip("/")
        return f"{base}/openai/v1/chat/completions"
