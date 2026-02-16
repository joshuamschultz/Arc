"""Base adapter — shared plumbing for all provider adapters."""

import json
import os
from typing import Any

import httpx

from arcllm.config import ModelMetadata, ProviderConfig
from arcllm.exceptions import ArcLLMConfigError, ArcLLMParseError
from arcllm.types import LLMProvider, LLMResponse, Message, Tool

DEFAULT_MAX_OUTPUT_TOKENS = 4096


class BaseAdapter(LLMProvider):
    """Concrete base class for provider adapters.

    Handles config storage, API key resolution, httpx client lifecycle,
    and async context manager support. Subclasses implement invoke().
    """

    def __init__(self, config: ProviderConfig, model_name: str) -> None:
        self._config = config
        self._model_name = model_name
        self._model_meta: ModelMetadata | None = config.models.get(model_name)

        env_var = config.provider.api_key_env
        api_key = os.environ.get(env_var, "")
        if config.provider.api_key_required and not api_key:
            raise ArcLLMConfigError(
                f"Missing environment variable '{env_var}' for provider. "
                "Set it to your API key."
            )
        self._api_key = api_key

        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))

    @property
    def name(self) -> str:
        return self._config.provider.api_format

    @property
    def model_name(self) -> str:
        return self._model_name

    def _parse_arguments(self, raw: Any) -> dict[str, Any]:
        """Parse tool call arguments from provider response.

        Handles dict (pass-through), str (JSON parse), or raises ArcLLMParseError.
        """
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except json.JSONDecodeError as e:
                raise ArcLLMParseError(raw_string=raw, original_error=e)
        raise ArcLLMParseError(
            raw_string=str(raw),
            original_error=TypeError(f"Unexpected arguments type: {type(raw)}"),
        )

    def _resolve_defaults(self, **kwargs: Any) -> tuple[int, float]:
        """Resolve max_tokens and temperature from kwargs, model meta, or config."""
        max_tokens = kwargs.get(
            "max_tokens",
            self._model_meta.max_output_tokens
            if self._model_meta
            else DEFAULT_MAX_OUTPUT_TOKENS,
        )
        temperature = kwargs.get(
            "temperature", self._config.provider.default_temperature
        )
        return max_tokens, temperature

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float | None:
        """Extract Retry-After header value as seconds, or None."""
        value = response.headers.get("retry-after")
        if value is None:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    def validate_config(self) -> bool:
        if not self._config.provider.api_key_required:
            return True
        return bool(self._api_key)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None  # type: ignore[assignment]

    async def __aenter__(self) -> "BaseAdapter":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
