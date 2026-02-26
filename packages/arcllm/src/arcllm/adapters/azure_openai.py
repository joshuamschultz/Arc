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

from typing import Any

from arcllm.adapters.openai import OpenaiAdapter
from arcllm.exceptions import ArcLLMAPIError
from arcllm.types import LLMResponse, Message, Tool


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

    async def invoke(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Send request to Azure OpenAI v1 API endpoint.

        URL: ``{base_url}/openai/v1/chat/completions`` — no query params.
        The v1 API hard-fails (400) if ``?api-version=`` is appended.
        """
        headers = self._build_headers()
        body = self._build_request_body(messages, tools, **kwargs)

        # rstrip('/') prevents double-slash when base_url has trailing slash
        base = self._config.provider.base_url.rstrip("/")
        url = f"{base}/openai/v1/chat/completions"

        response = await self._client.post(url, headers=headers, json=body)

        if response.status_code != 200:
            raise ArcLLMAPIError(
                status_code=response.status_code,
                body=response.text,
                provider=self.name,
                retry_after=self._parse_retry_after(response),
            )

        return self._parse_response(response.json())
