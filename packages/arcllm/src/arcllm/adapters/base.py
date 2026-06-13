"""Base adapter — shared plumbing for all provider adapters."""

import json
import os
from typing import Any

import httpx

from arcllm.config import ModelMetadata, ProviderConfig
from arcllm.exceptions import ArcLLMConfigError, ArcLLMParseError
from arcllm.types import LLMProvider

DEFAULT_MAX_OUTPUT_TOKENS = 4096


class BaseAdapter(LLMProvider):
    """Concrete base class for provider adapters.

    Handles config storage, API key resolution, httpx client lifecycle,
    and async context manager support. Subclasses implement invoke().
    """

    def __init__(
        self,
        config: ProviderConfig,
        model_name: str,
        resolved_api_key: str | None = None,
    ) -> None:
        self._config = config
        self._model_name = model_name
        self._model_meta: ModelMetadata | None = config.models.get(model_name)

        # Vault-resolved key takes priority over env var (keeps secrets
        # out of os.environ — NIST 800-53 AU-9, FedRAMP requirement)
        if resolved_api_key is not None:
            api_key = resolved_api_key
        else:
            env_var = config.provider.api_key_env
            api_key = os.environ.get(env_var, "")
            if config.provider.api_key_required and not api_key:
                raise ArcLLMConfigError(
                    f"Missing environment variable '{env_var}' for provider. "
                    f"Set it to your API key."
                )
        self._api_key = api_key

        # 180s send-side timeout. Multi-tool agentic turns (e.g. SCAP
        # demo's "build me the SC evidence package", which runs
        # baseline_compare + evidence_pack + a long synthesis pass)
        # routinely take 60-90s on a single LLM round-trip with a 100k
        # token context. The previous 60s default failed those legitimate
        # calls.
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(180.0))

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
                raise ArcLLMParseError(raw_string=raw, original_error=e) from e
        raise ArcLLMParseError(
            raw_string=str(raw),
            original_error=TypeError(f"Unexpected arguments type: {type(raw)}"),
        )

    def _resolve_defaults(self, **kwargs: Any) -> tuple[int, float]:
        """Resolve max_tokens and temperature from kwargs, model meta, or config."""
        max_tokens = kwargs.get(
            "max_tokens",
            self._model_meta.max_output_tokens if self._model_meta else DEFAULT_MAX_OUTPUT_TOKENS,
        )
        temperature = kwargs.get("temperature", self._config.provider.default_temperature)
        return max_tokens, temperature

    def _check_tool_capability(self, tools: Any) -> None:
        """Raise if the caller passed tools to a non-tool-capable model.

        Provider TOML declares per-model ``supports_tools``. A model
        marked ``False`` (or an unknown model with no metadata) silently
        ignores tool-call requests in production — the wire response
        carries a JSON-as-text content block instead of a tool_calls
        array. That's the most expensive class of arcllm bug to debug.

        Convert it to a loud failure at invoke time. Models with
        ``supports_tools = true`` and models the operator hasn't
        declared in TOML (no ``_model_meta``) are both allowed through
        — the latter so brand-new models that are tool-capable but not
        yet declared don't get blocked.
        """
        if not tools:
            return
        meta = self._model_meta
        if meta is None:
            # No declared metadata — trust the operator picked correctly.
            # The wire layer will surface the real failure if the model
            # truly can't carry tools.
            return
        if not meta.supports_tools:
            raise ArcLLMConfigError(
                f"Model {self._model_name!r} (provider {self.name!r}) is not "
                "marked tool-capable in provider metadata; passing tools= "
                "would silently fail. Use a different model or remove tools."
            )

    def _validate_response_format(self, rf: Any) -> dict[str, Any] | None:
        """Validate the ``response_format`` kwarg shape.

        Returns the validated dict ready to write into a provider
        request body, or ``None`` when the caller didn't pass one.
        Adapters that don't support a server-side JSON mode override
        this to raise ``ArcLLMConfigError``.

        Accepts these shapes:

        - ``None`` or missing                           — no-op
        - ``{"type": "text"}``                          — no-op (no enforcement)
        - ``{"type": "json_object"}``                   — provider must emit a JSON object
        - ``{"type": "json_schema", "json_schema": {...}}`` — schema-validated output

        Anything else raises ``ArcLLMConfigError`` immediately rather
        than silently degrading.
        """
        if rf is None:
            return None
        if not isinstance(rf, dict):
            raise ArcLLMConfigError(f"response_format must be a dict, got {type(rf).__name__}")
        rf_type = rf.get("type", "text")
        if rf_type not in {"text", "json_object", "json_schema"}:
            raise ArcLLMConfigError(
                f"response_format.type must be one of "
                f"'text'|'json_object'|'json_schema', got {rf_type!r}"
            )
        if rf_type == "text":
            return None
        if rf_type == "json_schema" and not isinstance(rf.get("json_schema"), dict):
            raise ArcLLMConfigError(
                "response_format.type='json_schema' requires a 'json_schema' dict"
            )
        # Strip unknown keys to keep the wire payload clean.
        clean: dict[str, Any] = {"type": rf_type}
        if rf_type == "json_schema":
            clean["json_schema"] = rf["json_schema"]
        return clean

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
            self._client = None  # type: ignore[assignment]  # reason: _client is typed httpx.AsyncClient (non-Optional) so it can be used unguarded after init; we set None on close to release the pool — pre-close use is forbidden by contract

    async def __aenter__(self) -> "BaseAdapter":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
