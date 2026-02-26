"""Google Gemini adapter — OpenAI-compatible endpoint with custom URL path."""

from typing import Any

from arcllm.adapters.openai import OpenaiAdapter
from arcllm.exceptions import ArcLLMAPIError
from arcllm.types import LLMResponse, Message, Tool


class GoogleAdapter(OpenaiAdapter):
    """Translates ArcLLM types to/from the Google Gemini OpenAI-compatible API.

    Google's endpoint uses ``/chat/completions`` directly under the base URL
    instead of the OpenAI convention of ``/v1/chat/completions``.
    """

    @property
    def name(self) -> str:
        return "google"

    async def invoke(
        self,
        messages: list[Message],
        tools: list[Tool] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        headers = self._build_headers()
        body = self._build_request_body(messages, tools, **kwargs)

        # Google's path: {base}/chat/completions (no /v1/ prefix)
        base = self._config.provider.base_url.rstrip("/")
        url = f"{base}/chat/completions"

        response = await self._client.post(url, headers=headers, json=body)

        if response.status_code != 200:
            raise ArcLLMAPIError(
                status_code=response.status_code,
                body=response.text,
                provider=self.name,
                retry_after=self._parse_retry_after(response),
            )

        return self._parse_response(response.json())
