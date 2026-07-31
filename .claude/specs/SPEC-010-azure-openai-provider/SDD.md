# SDD: Azure OpenAI Provider (Azure AI Foundry)

## Architecture

Unlike thin-alias adapters (DeepSeek, Groq, etc.), Azure OpenAI requires overriding URL construction and authentication. It subclasses `OpenaiAdapter` and overrides three methods.

```
OpenaiAdapter (base)
    ├── DeepseekAdapter    (thin alias — name only)
    ├── GroqAdapter        (thin alias — name only)
    ├── ...
    └── Azure_openaiAdapter  (URL + auth override)  <-- NEW
```

## Components

### 1. Adapter: `arcllm/adapters/azure_openai.py`

Subclasses `OpenaiAdapter`. Overrides:
- `name` property → `"azure_openai"`
- `_build_headers()` → uses `api-key` header instead of `Authorization: Bearer`
- `invoke()` → constructs Azure-specific URL (v1 API path)

```python
"""Azure OpenAI Service adapter — Azure AI Foundry / GCC deployment support.

Extends the OpenAI adapter with Azure-specific URL construction and
api-key header authentication. Supports both commercial (.azure.com)
and government (.azure.us) endpoints.
"""

from __future__ import annotations

from typing import Any

from arcllm.adapters.openai import OpenaiAdapter
from arcllm.config import ProviderConfig
from arcllm.exceptions import ArcLLMAPIError
from arcllm.types import LLMResponse, Message, Tool


class Azure_openaiAdapter(OpenaiAdapter):
    """Azure OpenAI Service adapter.

    Differences from standard OpenAI:
    - URL: {base}/openai/v1/chat/completions (v1 API, no api-version)
    - Auth: api-key header (not Authorization: Bearer)
    - Model field: deployment name, not canonical model name
    - content_filter finish_reason from Azure Content Safety
    """

    @property
    def name(self) -> str:
        return "azure_openai"

    def _build_headers(self) -> dict[str, str]:
        """Use api-key header instead of Bearer token."""
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
        """Send request to Azure OpenAI v1 API endpoint."""
        headers = self._build_headers()
        body = self._build_request_body(messages, tools, **kwargs)

        # Azure v1 API: {base_url}/openai/v1/chat/completions
        url = f"{self._config.provider.base_url}/openai/v1/chat/completions"

        response = await self._client.post(url, headers=headers, json=body)

        if response.status_code != 200:
            raise ArcLLMAPIError(
                status_code=response.status_code,
                body=response.text,
                provider=self.name,
                retry_after=self._parse_retry_after(response),
            )

        return self._parse_response(response.json())
```

### 2. Provider Config: `arcllm/providers/azure_openai.toml`

TOML configuration. The `base_url` is a placeholder — users MUST override it with their actual Azure resource endpoint (commercial or GCC).

Key differences from openai.toml:
- `api_format = "openai-chat"` (same response format)
- `base_url` = user's Azure resource endpoint
- `api_key_env = "AZURE_OPENAI_API_KEY"`
- `default_model` = deployment name (user-configured)
- Model metadata for GCC-available models

```toml
[provider]
api_format = "openai-chat"
base_url = "https://REPLACE-WITH-YOUR-RESOURCE.openai.azure.us"
api_key_env = "AZURE_OPENAI_API_KEY"
api_key_required = true
default_model = "gpt-4o"
default_temperature = 0.7
vault_path = ""

# NOTE: Model names here are canonical Azure model names.
# Users deploy these as "deployments" with custom names.
# The `model` field in load_model() should be the deployment name.
# These metadata entries are for pricing/capability reference.

[models.gpt-4o]
context_window = 128000
max_output_tokens = 16384
supports_tools = true
supports_vision = true
supports_thinking = false
input_modalities = ["text", "image"]
cost_input_per_1m = 2.50
cost_output_per_1m = 10.00
cost_cache_read_per_1m = 1.25
cost_cache_write_per_1m = 2.50

[models.gpt-4o-mini]
context_window = 128000
max_output_tokens = 16384
supports_tools = true
supports_vision = true
supports_thinking = false
input_modalities = ["text", "image"]
cost_input_per_1m = 0.15
cost_output_per_1m = 0.60
cost_cache_read_per_1m = 0.075
cost_cache_write_per_1m = 0.15

[models.gpt-4_1]
context_window = 300000
max_output_tokens = 32768
supports_tools = true
supports_vision = true
supports_thinking = false
input_modalities = ["text", "image"]
cost_input_per_1m = 2.00
cost_output_per_1m = 8.00
cost_cache_read_per_1m = 0.50
cost_cache_write_per_1m = 2.00

[models.gpt-4_1-mini]
context_window = 300000
max_output_tokens = 32768
supports_tools = true
supports_vision = true
supports_thinking = false
input_modalities = ["text", "image"]
cost_input_per_1m = 0.40
cost_output_per_1m = 1.60
cost_cache_read_per_1m = 0.10
cost_cache_write_per_1m = 0.40
```

### 3. Lazy Import: `arcllm/__init__.py`

Add `Azure_openaiAdapter` to `_LAZY_IMPORTS` dict and `__all__` list.

### 4. StopReason Mapping

The existing OpenAI adapter's `_STOP_REASON_MAP` already includes `"content_filter": "content_filter"`. No changes needed — Azure's `content_filter` finish_reason is already handled.

### 5. HTTPS Validation

The existing `ProviderSettings._validate_https` validator enforces HTTPS for all non-localhost URLs. Both `.azure.com` and `.azure.us` endpoints are HTTPS, so this works out of the box.

## Data Flow

```
load_model("azure_openai", "my-gpt4o-deployment")
    -> load_provider_config("azure_openai")      # reads azure_openai.toml
    -> _get_adapter_class("azure_openai")         # imports Azure_openaiAdapter
    -> Azure_openaiAdapter(config, "my-gpt4o-deployment")
    -> adapter.invoke(messages)
        -> POST {base_url}/openai/v1/chat/completions
           Headers: api-key: {key}
           Body: {"model": "my-gpt4o-deployment", "messages": [...]}
```

## Configuration Pattern

Users configure their Azure resource in one of two ways:

**Option A: Environment variable override (recommended for GCC)**
```bash
export AZURE_OPENAI_API_KEY="abc123..."
export ARCLLM_AZURE_OPENAI__BASE_URL="https://myresource.openai.azure.us"
```

**Option B: Custom TOML (copy and modify azure_openai.toml)**
The user overrides `base_url` and `default_model` in their config.

**In arcagent.toml:**
```toml
[llm]
model = "azure_openai/my-gpt4o-deployment"
```

## Security Considerations

- HTTPS enforced by existing `ProviderSettings._validate_https`
- API key from env var only (never filesystem) — vault path supported for production
- No new attack surface beyond existing OpenAI adapter
- GCC endpoints (.azure.us) are FedRAMP High authorized
- api-key header is standard Azure pattern (not less secure than Bearer)

## Dependencies

None new. Uses existing `httpx`, `pydantic`, and `arcllm` internals.

---

## Research Insights

### API Behavior Edge Cases

- **v1 API rejects `api-version`**: Passing `?api-version=` as a query parameter to the v1 endpoint is a **hard failure** (400 Bad Request), not silently ignored. The adapter MUST NOT append any query parameters.
- **Dual endpoint formats**: Both `.services.ai.azure.com` and `.openai.azure.com` accept the `/openai/v1/` path. The TOML placeholder uses `.openai.azure.us` for GCC.
- **DeploymentNotFound 404**: New deployments take up to 5 minutes to propagate. Users may see 404s immediately after creating a deployment. Not an adapter bug — document this.
- **Content filter null content**: When Azure Content Safety triggers, the `content` field in the response is `null` (not empty string). The response includes a `content_filter_results` object with severity categories (`hate`, `self_harm`, `sexual`, `violence`). The adapter should handle `null` content gracefully.

### Authentication Details

- **`api-key` header is case-sensitive**: Must be lowercase `api-key`, not `Api-Key` or `API-KEY`.
- **Entra ID auth error format differs**: If Entra (Azure AD) auth is configured instead of API key, error responses use a flat format (not nested like standard OpenAI errors). Out of scope for v1 but worth noting for future Managed Identity support.
- **GCC token scope**: For future Entra auth, the scope is `https://cognitiveservices.azure.us/.default` (not `.azure.com`).

### Rate Limiting

- **Per-deployment TPM/RPM**: Rate limits are set per deployment, not per resource. The `Retry-After` header is returned on 429 responses — already handled by `BaseAdapter._parse_retry_after()`.
- **`max_tokens` affects rate limit estimation**: Azure pre-allocates capacity based on `max_tokens` in the request. Setting unnecessarily high `max_tokens` can trigger rate limits earlier than expected.

### Error Format

- **Azure-specific error codes**: `DeploymentNotFound`, `ResponsibleAIPolicyViolation`, `content_filter_error`. These come in the `error.code` field. The base `ArcLLMAPIError` captures `body` as text, which is sufficient.
- **Deployment name validation**: Names must be alphanumeric + hyphens + underscores, 2-64 chars. No validation needed in the adapter (Azure rejects invalid names).

### Class Naming Convention

- **`Azure_openaiAdapter`** with `# noqa: N801` follows the established precedent of `Huggingface_TgiAdapter`. The `.title()` convention in `registry.py` produces this automatically from `azure_openai`.
- **`_LAZY_IMPORTS` key**: `"Azure_openaiAdapter": "arcllm.adapters.azure_openai"` matches the pattern of `"Huggingface_TgiAdapter": "arcllm.adapters.huggingface_tgi"`.

### Content Filter — Three Distinct Scenarios

Azure Content Safety can trigger in three ways, not two:

1. **Prompt blocked (HTTP 400)**: Input violates policy. Error code `ResponsibleAIPolicyViolation`. No choices returned. Must raise `ArcLLMAPIError`.
2. **Output blocked (HTTP 200)**: Response generated but filtered. `choices[0].message.content` is `null`, `finish_reason` is `"content_filter"`. The `content_filter_results` object has severity ratings. Already mapped in `_STOP_REASON_MAP`.
3. **Filter unavailable (HTTP 200)**: Content filter service error. Response includes `content_filter_results.error` sub-object but content may still be present. Rare but possible. Base parser handles this (content is not null).

### GCC-Specific Constraints

- **Context cap**: GCC models max at ~300,000 tokens (not 1M like commercial Azure). `gpt-4_1` / `gpt-4_1-mini` list 300k in provider TOML.
- **Model retirement**: GCC models retire earlier than commercial. `gpt-4o-0513` and `gpt-4o-mini-0718` retire ~March 31, 2026.
- **Deployment names**: 2-64 chars, alphanumeric + hyphens + underscores, must start with alphanumeric. Azure rejects invalid names — no adapter-side validation needed.

### httpx base_url Behavior

- httpx internally adds a trailing slash to `base_url` during `Client()` construction. Leading slash in relative paths drops the base path.
- Since `invoke()` builds the full URL with f-string (`f"{base_url}/openai/v1/..."`) rather than using httpx relative resolution, this is not an issue — but document for future maintainers.
- `rstrip('/')` on base_url is safe for all Azure URL patterns. Edge case: bare scheme `"https://".rstrip("/")` produces `"https:"` — not a concern since real Azure URLs always have a hostname.
